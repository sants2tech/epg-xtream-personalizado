#!/usr/bin/env python3
"""Gera XMLTV personalizado a partir dos canais de uma conta Xtream."""

import csv
import gzip
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

OUT = Path("public/epg.xml")
CONFIG = json.loads(Path("config.json").read_text(encoding="utf-8"))
MANUAL_FILE = Path("mapeamento_manual.json")


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def clean_base_url(value):
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("XTREAM_BASE_URL deve ser uma URL http(s) válida.")
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def request_bytes(session, url, params):
    response = session.get(
        url, params=params, timeout=CONFIG["request_timeout_seconds"]
    )
    response.raise_for_status()
    data = response.content
    return gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.casefold()
    value = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\b(hd|fhd|uhd|4k|sd|br|brasil)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def xtream_get(session, base, username, password, **params):
    return session.get(
        f"{base}/player_api.php",
        params={"username": username, "password": password, **params},
        timeout=CONFIG["request_timeout_seconds"],
    )


def parse_xmltv(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(f"XMLTV inválido: {exc}") from exc
    if root.tag != "tv":
        raise RuntimeError("O documento recebido não possui raiz XMLTV <tv>.")
    return root


def programme_key(programme):
    return (
        programme.get("channel"),
        programme.get("start"),
        programme.get("stop"),
        (programme.findtext("title") or "").strip(),
    )


def add_text(parent, tag, value):
    child = ET.SubElement(parent, tag)
    child.text = str(value or "")
    return child


def load_manual_mapping(channels, source_root):
    """Retorna mapa XMLTV ID -> Xtream stream_id."""
    if not MANUAL_FILE.exists():
        return {}, 0

    try:
        manual = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Erro lendo {MANUAL_FILE}: {exc}") from exc

    if not isinstance(manual, dict):
        raise RuntimeError("mapeamento_manual.json precisa conter um objeto JSON.")

    valid_streams = {
        str(ch.get("stream_id", "")).strip()
        for ch in channels
    }
    valid_xmltv_ids = {
        ch.get("id", "") for ch in source_root.findall("channel")
    }

    result = {}
    for stream_id, xmltv_id in manual.items():
        if str(stream_id).startswith("_"):
            continue
        stream_id = str(stream_id).strip()
        xmltv_id = str(xmltv_id).strip()
        if stream_id in valid_streams and xmltv_id in valid_xmltv_ids:
            result[xmltv_id] = stream_id

    return result, len(result)


def main():
    base = clean_base_url(required_env("XTREAM_BASE_URL"))
    username = required_env("XTREAM_USERNAME")
    password = required_env("XTREAM_PASSWORD")

    session = requests.Session()
    session.headers.update({"User-Agent": "EPG-Xtream-Personalizado/1.0"})

    response = xtream_get(session, base, username, password)
    response.raise_for_status()
    auth = response.json()
    user_info = auth.get("user_info", {})
    if user_info and str(user_info.get("auth", "1")) == "0":
        raise RuntimeError("A autenticação Xtream foi recusada.")

    response = xtream_get(
        session, base, username, password, action="get_live_streams"
    )
    response.raise_for_status()
    channels = response.json()
    if not isinstance(channels, list) or not channels:
        raise RuntimeError("A API não retornou canais ao vivo; EPG anterior preservado.")

    id_to_channel = {}
    name_to_ids = {}
    for ch in channels:
        stream_id = str(ch.get("stream_id", "")).strip()
        name = (ch.get("name") or f"Canal {stream_id}").strip()
        candidates = {
            str(ch.get("epg_channel_id") or "").strip(),
            str(ch.get("custom_sid") or "").strip(),
        }
        for candidate in candidates:
            if candidate:
                id_to_channel[candidate] = (stream_id, name, ch)
        norm = normalize(name)
        if norm:
            name_to_ids.setdefault(norm, []).append((stream_id, name, ch))

    xml_data = request_bytes(
        session,
        f"{base}/xmltv.php",
        {
            "username": username,
            "password": password,
            "prev_days": CONFIG["prev_days"],
            "next_days": CONFIG["next_days"],
        },
    )
    source_root = parse_xmltv(xml_data)

    source_id_map = {}
    id_matches = exact_matches = fuzzy_matches = 0
    fuzzy_candidates = [
        (norm, entries[0])
        for norm, entries in name_to_ids.items()
        if len(norm) >= 5 and len({entry[0] for entry in entries}) == 1
    ]

    # Correspondência automática por ID, nome exato e nome aproximado.
    for source_channel in source_root.findall("channel"):
        source_id = source_channel.get("id", "")
        target = id_to_channel.get(source_id)

        if target:
            source_id_map[source_id] = target[0]
            id_matches += 1
            continue

        display_names = [
            item.text or "" for item in source_channel.findall("display-name")
        ]
        target = None

        for display_name in display_names:
            matches = name_to_ids.get(normalize(display_name), [])
            unique_streams = {entry[0] for entry in matches}
            if matches and len(unique_streams) == 1:
                target = matches[0]
                exact_matches += 1
                break

        if target is None:
            for display_name in display_names:
                norm = normalize(display_name)
                if len(norm) < 5:
                    continue
                ranked = sorted(
                    [
                        (
                            SequenceMatcher(None, norm, candidate_norm).ratio(),
                            entry,
                        )
                        for candidate_norm, entry in fuzzy_candidates
                    ],
                    key=lambda item: item[0],
                    reverse=True,
                )
                if not ranked:
                    continue
                best_score, best_entry = ranked[0]
                second_score = ranked[1][0] if len(ranked) > 1 else 0.0
                if best_score >= 0.93 and best_score - second_score >= 0.08:
                    target = best_entry
                    fuzzy_matches += 1
                    break

        if target:
            source_id_map[source_id] = target[0]

    # O mapeamento manual tem prioridade sobre o automático.
    manual_map, manual_matches = load_manual_mapping(channels, source_root)
    source_id_map.update(manual_map)

    out = ET.Element(
        "tv",
        {
            "generator-info-name": "EPG Xtream Personalizado",
            "generator-info-url": "https://github.com/sants2tech/epg-xtream-personalizado",
        },
    )

    emitted = set()
    for ch in channels:
        sid = str(ch.get("stream_id", "")).strip()
        if not sid or sid in emitted:
            continue
        emitted.add(sid)
        node = ET.SubElement(out, "channel", {"id": sid})
        add_text(node, "display-name", ch.get("name") or f"Canal {sid}")
        logo = (ch.get("stream_icon") or "").strip()
        if logo.startswith(("http://", "https://")):
            ET.SubElement(node, "icon", {"src": logo})

    # Copia os programas XMLTV e contabiliza quantos cada canal recebeu.
    count = 0
    seen = set()
    programmes_per_channel = {}
    for programme in source_root.findall("programme"):
        source_id = programme.get("channel", "")
        target_id = source_id_map.get(source_id)
        if not target_id:
            continue
        copied = ET.fromstring(ET.tostring(programme, encoding="utf-8"))
        copied.set("channel", target_id)
        key = programme_key(copied)
        if key in seen:
            continue
        seen.add(key)
        out.append(copied)
        count += 1
        programmes_per_channel[target_id] = programmes_per_channel.get(target_id, 0) + 1

    # Consulta EPG curto apenas para canais que ainda não receberam programas.
    fallback_count = 0
    if CONFIG.get("fallback_short_epg", True):
        for ch in channels:
            sid = str(ch.get("stream_id", "")).strip()
            if not sid or programmes_per_channel.get(sid, 0) > 0:
                continue
            try:
                short = xtream_get(
                    session,
                    base,
                    username,
                    password,
                    action="get_short_epg",
                    stream_id=sid,
                    limit=CONFIG["max_programmes_per_channel"],
                )
                short.raise_for_status()
                payload = short.json()
                listings = payload.get("epg_listings", []) if isinstance(payload, dict) else []

                for item in listings:
                    start = item.get("start_timestamp")
                    stop = item.get("stop_timestamp")
                    if not start or not stop:
                        continue

                    start_dt = datetime.fromtimestamp(int(start), timezone.utc)
                    stop_dt = datetime.fromtimestamp(int(stop), timezone.utc)
                    programme = ET.Element(
                        "programme",
                        {
                            "start": start_dt.strftime("%Y%m%d%H%M%S +0000"),
                            "stop": stop_dt.strftime("%Y%m%d%H%M%S +0000"),
                            "channel": sid,
                        },
                    )
                    add_text(
                        programme,
                        "title",
                        item.get("title") or item.get("name") or "Programação",
                    )
                    description = item.get("description") or item.get("plot") or ""
                    if description:
                        add_text(programme, "desc", description)

                    key = programme_key(programme)
                    if key not in seen:
                        seen.add(key)
                        out.append(programme)
                        count += 1
                        fallback_count += 1
                        programmes_per_channel[sid] = programmes_per_channel.get(sid, 0) + 1

            except (requests.RequestException, ValueError, TypeError) as exc:
                print(f"Aviso: EPG curto indisponível para stream_id={sid}: {exc}")

    # Relatórios CSV para facilitar a criação de novos mapeamentos manuais.
    OUT.parent.mkdir(parents=True, exist_ok=True)

    with (OUT.parent / "canais_sem_epg.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["stream_id", "nome_xtream", "epg_channel_id", "programas"])
        for ch in channels:
            sid = str(ch.get("stream_id", "")).strip()
            if sid and programmes_per_channel.get(sid, 0) == 0:
                writer.writerow([
                    sid,
                    ch.get("name", ""),
                    ch.get("epg_channel_id", ""),
                    0,
                ])

    with (OUT.parent / "canais_xmltv_disponiveis.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["xmltv_id", "nome_fonte", "stream_id_associado"])
        for source_channel in source_root.findall("channel"):
            source_id = source_channel.get("id", "")
            names = [
                item.text or "" for item in source_channel.findall("display-name")
            ]
            writer.writerow([
                source_id,
                " / ".join(names),
                source_id_map.get(source_id, ""),
            ])

    if count == 0:
        raise RuntimeError(
            "Nenhum programa foi encontrado. O XMLTV anterior foi preservado; "
            "verifique se o provedor fornece EPG."
        )

    tmp = OUT.with_suffix(".xml.tmp")
    ET.indent(out, space="  ")
    ET.ElementTree(out).write(tmp, encoding="utf-8", xml_declaration=True)
    tmp.replace(OUT)

    mapped = len(set(source_id_map.values()))
    channels_with_epg = len(programmes_per_channel)
    print(
        f"EPG gerado: {len(channels)} canais; {channels_with_epg} com programação; "
        f"{mapped} associados ao XMLTV; {count} programas."
    )
    print(f"Correspondências por ID: {id_matches}")
    print(f"Correspondências por nome exato: {exact_matches}")
    print(f"Correspondências aproximadas: {fuzzy_matches}")
    print(f"Correspondências manuais: {manual_matches}")
    print(f"Programas obtidos pelo EPG curto: {fallback_count}")
    print(f"Canais sem EPG: {len(channels) - channels_with_epg}")
    print(f"Relatório: {OUT.parent / 'canais_sem_epg.csv'}")
    print(f"IDs XMLTV disponíveis: {OUT.parent / 'canais_xmltv_disponiveis.csv'}")
    print(f"Arquivo: {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
