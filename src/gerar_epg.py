
#!/usr/bin/env python3
"""Gera um XMLTV filtrado pelos canais da conta Xtream configurada em variáveis de ambiente."""

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


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def clean_base_url(value):
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(
            "XTREAM_BASE_URL deve ser uma URL http(s), "
            "por exemplo https://servidor:porta"
        )
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def request_bytes(session, url, params):
    response = session.get(
        url,
        params=params,
        timeout=CONFIG["request_timeout_seconds"]
    )
    response.raise_for_status()

    data = response.content

    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)

    return data


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(
        c for c in value if not unicodedata.combining(c)
    )
    value = value.casefold()
    value = re.sub(r"\[[^]]*\]|\([^)]*\)", " ", value)
    value = re.sub(
        r"\b(hd|fhd|uhd|4k|sd|br|brasil)\b",
        " ",
        value
    )
    return re.sub(r"[^a-z0-9]+", "", value)


def xtream_get(session, base, username, password, **params):
    query = {
        "username": username,
        "password": password,
        **params
    }

    return session.get(
        f"{base}/player_api.php",
        params=query,
        timeout=CONFIG["request_timeout_seconds"]
    )


def parse_xmltv(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"O servidor não retornou XMLTV válido: {exc}"
        ) from exc

    if root.tag != "tv":
        raise RuntimeError(
            "O documento recebido não possui raiz XMLTV <tv>."
        )

    return root


def programme_key(p):
    return (
        p.get("channel"),
        p.get("start"),
        p.get("stop"),
        (p.findtext("title") or "").strip()
    )


def add_text(parent, tag, value):
    child = ET.SubElement(parent, tag)
    child.text = str(value or "")
    return child


def main():
    base = clean_base_url(required_env("XTREAM_BASE_URL"))
    username = required_env("XTREAM_USERNAME")
    password = required_env("XTREAM_PASSWORD")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "EPG-Xtream-Personalizado/1.0"
    })

    # Verificar autenticação Xtream.
    r = xtream_get(session, base, username, password)
    r.raise_for_status()

    auth = r.json()
    user_info = auth.get("user_info", {})

    if user_info and str(user_info.get("auth", "1")) == "0":
        raise RuntimeError(
            "A autenticação Xtream foi recusada. "
            "Confira os GitHub Secrets."
        )

    # Buscar canais ao vivo.
    r = xtream_get(
        session,
        base,
        username,
        password,
        action="get_live_streams"
    )
    r.raise_for_status()

    channels = r.json()

    if not isinstance(channels, list) or not channels:
        raise RuntimeError(
            "A API não retornou canais ao vivo. "
            "XMLTV anterior foi preservado."
        )

    # Preparar IDs e nomes dos canais Xtream.
    id_to_channel = {}
    name_to_ids = {}

    for ch in channels:
        stream_id = str(ch.get("stream_id", "")).strip()
        name = (ch.get("name") or f"Canal {stream_id}").strip()

        epg_id = str(ch.get("epg_channel_id") or "").strip()

        candidates = {
            epg_id,
            str(ch.get("custom_sid") or "").strip()
        }

        for candidate in candidates:
            if candidate:
                id_to_channel[candidate] = (
                    stream_id,
                    name,
                    ch
                )

        norm = normalize(name)

        if norm:
            name_to_ids.setdefault(norm, []).append(
                (stream_id, name, ch)
            )

    # Baixar XMLTV do provedor.
    xmltv_url = f"{base}/xmltv.php"

    xml_data = request_bytes(
        session,
        xmltv_url,
        {
            "username": username,
            "password": password,
            "prev_days": CONFIG["prev_days"],
            "next_days": CONFIG["next_days"],
        }
    )

    source_root = parse_xmltv(xml_data)

    out = ET.Element(
        "tv",
        {
            "generator-info-name": "EPG Xtream Personalizado",
            "generator-info-url": "https://github.com/",
        }
    )

    # --------------------------------------------------
    # MAPEAMENTO INTELIGENTE DOS CANAIS
    # Prioridade:
    # 1. ID exato
    # 2. Nome exato normalizado
    # 3. Nome semelhante (mínimo 93%)
    # --------------------------------------------------

    source_id_map = {}

    exact_matches = 0
    fuzzy_matches = 0
    id_matches = 0

    fuzzy_candidates = [
        (norm, entries[0])
        for norm, entries in name_to_ids.items()
        if len(norm) >= 5
        and len({entry[0] for entry in entries}) == 1
    ]

    for source_channel in source_root.findall("channel"):

        source_id = source_channel.get("id", "")
        target = id_to_channel.get(source_id)

        # 1. Correspondência por ID.
        if target:
            source_id_map[source_id] = target[0]
            id_matches += 1
            continue

        display_names = [
            x.text or ""
            for x in source_channel.findall("display-name")
        ]

        # 2. Correspondência por nome exato.
        for display_name in display_names:

            norm = normalize(display_name)
            matches = name_to_ids.get(norm, [])

            unique_streams = {
                entry[0] for entry in matches
            }

            if matches and len(unique_streams) == 1:
                target = matches[0]
                exact_matches += 1
                break

        # 3. Correspondência aproximada.
        if not target:

            for display_name in display_names:

                norm = normalize(display_name)

                if len(norm) < 5:
                    continue

                ranked = sorted(
                    (
                        (
                            SequenceMatcher(
                                None,
                                norm,
                                candidate_norm
                            ).ratio(),
                            entry
                        )
                        for candidate_norm, entry in fuzzy_candidates
                    ),
                    key=lambda item: item[0],
                    reverse=True
                )

                if not ranked:
                    continue

                best_score, best_entry = ranked[0]

                second_score = (
                    ranked[1][0]
                    if len(ranked) > 1
                    else 0.0
                )

                # Exige 93% de similaridade e margem
                # mínima de 8% sobre o segundo candidato.
                if (
                    best_score >= 0.93
                    and best_score - second_score >= 0.08
                ):
                    target = best_entry
                    fuzzy_matches += 1
                    break

        if target:
            source_id_map[source_id] = target[0]

    # --------------------------------------------------
    # INCLUIR TODOS OS CANAIS DA LISTA XTREAM
    # --------------------------------------------------

    emitted = set()

    for ch in channels:

        sid = str(ch.get("stream_id", "")).strip()

        if not sid or sid in emitted:
            continue

        emitted.add(sid)

        node = ET.SubElement(
            out,
            "channel",
            {"id": sid}
        )

        add_text(
            node,
            "display-name",
            ch.get("name") or f"Canal {sid}"
        )

        logo = (ch.get("stream_icon") or "").strip()

        if logo.startswith(("http://", "https://")):
            ET.SubElement(
                node,
                "icon",
                {"src": logo}
            )

    # --------------------------------------------------
    # COPIAR A PROGRAMAÇÃO XMLTV
    # --------------------------------------------------

    count = 0
    seen = set()

    for programme in source_root.findall("programme"):

        source_id = programme.get("channel", "")
        target_id = source_id_map.get(source_id)

        if not target_id:
            continue

        copied = ET.fromstring(
            ET.tostring(
                programme,
                encoding="utf-8"
            )
        )

        copied.set("channel", target_id)

        key = programme_key(copied)

        if key in seen:
            continue

        seen.add(key)
        out.append(copied)
        count += 1

    # --------------------------------------------------
    # EPG CURTO COMO ALTERNATIVA
    # --------------------------------------------------

    if count == 0 and CONFIG.get("fallback_short_epg", True):

        for ch in channels:

            sid = str(ch.get("stream_id", "")).strip()

            if not sid:
                continue

            try:

                short = xtream_get(
                    session,
                    base,
                    username,
                    password,
                    action="get_short_epg",
                    stream_id=sid,
                    limit=CONFIG["max_programmes_per_channel"]
                )

                short.raise_for_status()

                payload = short.json()

                listings = (
                    payload.get("epg_listings", [])
                    if isinstance(payload, dict)
                    else []
                )

                for item in listings:

                    start = item.get("start_timestamp")
                    stop = item.get("stop_timestamp")

                    if start and stop:

                        start_dt = datetime.fromtimestamp(
                            int(start),
                            timezone.utc
                        )

                        stop_dt = datetime.fromtimestamp(
                            int(stop),
                            timezone.utc
                        )

                        start_s = start_dt.strftime(
                            "%Y%m%d%H%M%S +0000"
                        )

                        stop_s = stop_dt.strftime(
                            "%Y%m%d%H%M%S +0000"
                        )

                    else:
                        continue

                    title = (
                        item.get("title")
                        or item.get("name")
                        or "Programação"
                    )

                    p = ET.Element(
                        "programme",
                        {
                            "start": start_s,
                            "stop": stop_s,
                            "channel": sid
                        }
                    )

                    add_text(p, "title", title)

                    desc = (
                        item.get("description")
                        or item.get("plot")
                        or ""
                    )

                    if desc:
                        add_text(p, "desc", desc)

                    key = programme_key(p)

                    if key not in seen:
                        seen.add(key)
                        out.append(p)
                        count += 1

            except requests.RequestException as exc:

                print(
                    f"Aviso: EPG curto indisponível "
                    f"para stream_id={sid}: {exc}"
                )

    # Não substituir o EPG anterior se não houver programação.
    if count == 0:
        raise RuntimeError(
            "Nenhum programa foi encontrado. "
            "O XMLTV anterior foi preservado; "
            "verifique se o provedor fornece EPG."
        )

    # --------------------------------------------------
    # GERAR ARQUIVO XMLTV
    # --------------------------------------------------

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = OUT.with_suffix(".xml.tmp")

    ET.indent(out, space="  ")

    ET.ElementTree(out).write(
        tmp,
        encoding="utf-8",
        xml_declaration=True
    )

    tmp.replace(OUT)

    # --------------------------------------------------
    # RELATÓRIO FINAL
    # --------------------------------------------------

    mapped = len(source_id_map)

    print(
        f"EPG gerado: {len(channels)} canais; "
        f"{mapped} canais associados por XMLTV; "
        f"{count} programas."
    )

    print(f"Correspondências por ID: {id_matches}")
    print(f"Correspondências por nome exato: {exact_matches}")
    print(f"Correspondências aproximadas: {fuzzy_matches}")
    print(f"Arquivo: {OUT}")


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            f"ERRO: {exc}",
            file=sys.stderr
        )

        sys.exit(1)
