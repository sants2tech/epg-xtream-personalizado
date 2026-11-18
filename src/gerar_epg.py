
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

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
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
        raise ValueError(
            "XTREAM_BASE_URL deve ser uma URL http(s) válida."
        )

    return urlunsplit(
        (p.scheme, p.netloc, p.path.rstrip("/"), "", "")
    )


def request_bytes(session, url, params):
    response = session.get(
        url,
        params=params,
        timeout=CONFIG["request_timeout_seconds"]
    )

    response.raise_for_status()
    data = response.content

    return (
        gzip.decompress(data)
        if data.startswith(b"\x1f\x8b")
        else data
    )


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.casefold()

    value = re.sub(
        r"\[[^]]*\]|\([^)]*\)",
        " ",
        value
    )

    value = re.sub(
        r"\b(hd|fhd|uhd|4k|sd|br|brasil)\b",
        " ",
        value
    )

    return re.sub(r"[^a-z0-9]+", "", value)


def xtream_get(session, base, username, password, **params):
    return session.get(
        f"{base}/player_api.php",
        params={
            "username": username,
            "password": password,
            **params
        },
        timeout=CONFIG["request_timeout_seconds"]
    )


def parse_xmltv(data):
    try:
        root = ET.fromstring(data)

    except ET.ParseError as exc:
        raise RuntimeError(
            f"XMLTV inválido: {exc}"
        ) from exc

    if root.tag != "tv":
        raise RuntimeError(
            "O documento recebido não possui raiz XMLTV <tv>."
        )

    return root


def parse_xmltv_datetime(value):
    """Converte timestamp XMLTV para UTC."""
    match = re.fullmatch(
        r"(\d{14})(?:\s*([+-])(\d{2}):?(\d{2}))?",
        str(value or "").strip()
    )
    if not match:
        return None

    dt = datetime.strptime(
        match.group(1),
        "%Y%m%d%H%M%S"
    ).replace(tzinfo=timezone.utc)

    sign, hours, minutes = match.group(2), match.group(3), match.group(4)
    if sign:
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        dt = dt - offset if sign == "+" else dt + offset

    return dt


def fetch_external_xmltv(session, source):
    """Baixa e valida uma fonte XMLTV externa."""
    url = str(source.get("url", "")).strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("URL XMLTV externa inválida.")

    response = session.get(
        url,
        timeout=CONFIG["request_timeout_seconds"],
        headers={"Accept": "application/xml,text/xml,*/*"}
    )
    response.raise_for_status()
    data = response.content

    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)

    return parse_xmltv(data)


def match_external_channel(source_channel, id_to_channels,
                           name_to_channels, fuzzy_candidates,
                           threshold):
    """Associa canal externo por ID, nome exato ou nome muito próximo."""
    source_id = str(source_channel.get("id", "")).strip()
    direct = id_to_channels.get(source_id, [])
    if direct:
        return list(dict.fromkeys(direct)), "id"

    display_names = [
        (item.text or "").strip()
        for item in source_channel.findall("display-name")
    ]

    exact = []
    for display_name in display_names:
        norm = normalize(display_name)
        if norm and norm in name_to_channels:
            exact.extend(name_to_channels[norm])

    if exact:
        return list(dict.fromkeys(exact)), "nome"

    best_score = 0.0
    second_score = 0.0
    best_streams = []

    for display_name in display_names:
        norm = normalize(display_name)
        if len(norm) < 5:
            continue

        ranked = []
        for candidate_norm, stream_ids in fuzzy_candidates:
            score = SequenceMatcher(None, norm, candidate_norm).ratio()
            ranked.append((score, stream_ids))

        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            continue

        score = ranked[0][0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0

        if score > best_score:
            best_score = score
            second_score = second
            best_streams = ranked[0][1]

    if best_score >= threshold and best_score - second_score >= 0.08:
        return list(dict.fromkeys(best_streams)), "aproximado"

    return [], "sem_correspondencia"


def programme_key(programme):
    return (
        programme.get("channel"),
        programme.get("start"),
        programme.get("stop"),
        (programme.findtext("title") or "").strip()
    )


def add_text(parent, tag, value):
    child = ET.SubElement(parent, tag)
    child.text = str(value or "")
    return child


def add_mapping(mapping, xmltv_id, stream_id):
    """Adiciona vários canais Xtream ao mesmo ID XMLTV."""

    xmltv_id = str(xmltv_id or "").strip()
    stream_id = str(stream_id or "").strip()

    if not xmltv_id or not stream_id:
        return

    if xmltv_id not in mapping:
        mapping[xmltv_id] = []

    if stream_id not in mapping[xmltv_id]:
        mapping[xmltv_id].append(stream_id)


def load_manual_mapping(channels, source_root):
    """
    Lê o mapeamento manual.

    Formato mantido:
    {
        "stream_id": "xmltv_id"
    }

    Vários stream_ids podem utilizar o mesmo xmltv_id.
    """

    if not MANUAL_FILE.exists():
        return {}, 0

    try:
        manual = json.loads(
            MANUAL_FILE.read_text(encoding="utf-8")
        )

    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Erro lendo {MANUAL_FILE}: {exc}"
        ) from exc

    if not isinstance(manual, dict):
        raise RuntimeError(
            "mapeamento_manual.json precisa conter um objeto JSON."
        )

    valid_streams = {
        str(ch.get("stream_id", "")).strip()
        for ch in channels
    }

    valid_xmltv_ids = {
        str(ch.get("id", "")).strip()
        for ch in source_root.findall("channel")
    }

    result = {}
    total = 0

    for stream_id, xmltv_id in manual.items():

        if str(stream_id).startswith("_"):
            continue

        stream_id = str(stream_id).strip()
        xmltv_id = str(xmltv_id).strip()

        if (
            stream_id in valid_streams
            and xmltv_id in valid_xmltv_ids
        ):
            add_mapping(result, xmltv_id, stream_id)
            total += 1

    return result, total


def fetch_short_epg(base, username, password, sid, limit):
    """Consulta individualmente o EPG curto de um canal."""

    try:
        response = requests.get(
            f"{base}/player_api.php",
            params={
                "username": username,
                "password": password,
                "action": "get_short_epg",
                "stream_id": sid,
                "limit": limit
            },
            timeout=(5, 12),
            headers={
                "User-Agent": "EPG-Xtream-Personalizado/2.0"
            }
        )

        response.raise_for_status()
        payload = response.json()

        listings = (
            payload.get("epg_listings", [])
            if isinstance(payload, dict)
            else []
        )

        return sid, listings, None

    except (
        requests.RequestException,
        ValueError,
        TypeError
    ) as exc:

        return sid, [], str(exc)


def main():

    print("Iniciando gerador EPG atualizado...", flush=True)

    base = clean_base_url(
        required_env("XTREAM_BASE_URL")
    )

    username = required_env("XTREAM_USERNAME")
    password = required_env("XTREAM_PASSWORD")

    session = requests.Session()

    session.headers.update({
        "User-Agent": "EPG-Xtream-Personalizado/2.0"
    })

    # --------------------------------------------------
    # AUTENTICAÇÃO
    # --------------------------------------------------

    response = xtream_get(
        session,
        base,
        username,
        password
    )

    response.raise_for_status()
    auth = response.json()

    user_info = auth.get("user_info", {})

    if (
        user_info
        and str(user_info.get("auth", "1")) == "0"
    ):
        raise RuntimeError(
            "A autenticação Xtream foi recusada."
        )

    # --------------------------------------------------
    # BUSCAR CANAIS XTREAM
    # --------------------------------------------------

    response = xtream_get(
        session,
        base,
        username,
        password,
        action="get_live_streams"
    )

    response.raise_for_status()
    channels = response.json()

    if not isinstance(channels, list) or not channels:
        raise RuntimeError(
            "A API não retornou canais ao vivo. "
            "O EPG anterior foi preservado."
        )

    print(
        f"Total de canais Xtream: {len(channels)}",
        flush=True
    )

    # --------------------------------------------------
    # ÍNDICES DE CANAIS
    # --------------------------------------------------

    id_to_channels = defaultdict(list)
    name_to_channels = defaultdict(list)

    valid_streams = set()

    for ch in channels:

        stream_id = str(
            ch.get("stream_id", "")
        ).strip()

        if not stream_id:
            continue

        valid_streams.add(stream_id)

        name = (
            ch.get("name")
            or f"Canal {stream_id}"
        ).strip()

        candidates = {
            str(ch.get("epg_channel_id") or "").strip(),
            str(ch.get("custom_sid") or "").strip()
        }

        for candidate in candidates:

            if candidate:
                if stream_id not in id_to_channels[candidate]:
                    id_to_channels[candidate].append(stream_id)

        norm = normalize(name)

        if norm:
            if stream_id not in name_to_channels[norm]:
                name_to_channels[norm].append(stream_id)

    # --------------------------------------------------
    # BAIXAR XMLTV ORIGINAL
    # --------------------------------------------------

    print("Baixando XMLTV do provedor...", flush=True)

    xml_data = request_bytes(
        session,
        f"{base}/xmltv.php",
        {
            "username": username,
            "password": password,
            "prev_days": CONFIG["prev_days"],
            "next_days": CONFIG["next_days"]
        }
    )

    source_root = parse_xmltv(xml_data)

    source_channels = source_root.findall("channel")
    source_programmes = source_root.findall("programme")

    print(
        f"IDs XMLTV encontrados: {len(source_channels)}",
        flush=True
    )

    # --------------------------------------------------
    # MAPEAMENTO AUTOMÁTICO
    # --------------------------------------------------

    source_id_map = {}

    id_matches = 0
    exact_matches = 0
    fuzzy_matches = 0

    fuzzy_candidates = [
        (norm, stream_ids)
        for norm, stream_ids in name_to_channels.items()
        if len(norm) >= 5
    ]

    print("Associando IDs XMLTV aos canais...", flush=True)

    for source_channel in source_channels:

        source_id = str(
            source_channel.get("id", "")
        ).strip()

        if not source_id:
            continue

        # Primeiro tenta correspondência direta por ID.
        target_ids = id_to_channels.get(source_id, [])

        if target_ids:

            for sid in target_ids:
                add_mapping(source_id_map, source_id, sid)

            id_matches += 1
            continue

        display_names = [
            item.text or ""
            for item in source_channel.findall("display-name")
        ]

        target_ids = []

        # Segundo: correspondência exata por nome.
        for display_name in display_names:

            norm = normalize(display_name)

            matches = name_to_channels.get(norm, [])

            if matches:

                target_ids = list(matches)
                exact_matches += 1
                break

        # Terceiro: correspondência aproximada.
        if not target_ids:

            best_score = 0.0
            second_score = 0.0
            best_streams = []

            for display_name in display_names:

                norm = normalize(display_name)

                if len(norm) < 5:
                    continue

                ranked = []

                for candidate_norm, stream_ids in fuzzy_candidates:

                    score = SequenceMatcher(
                        None,
                        norm,
                        candidate_norm
                    ).ratio()

                    ranked.append(
                        (score, stream_ids)
                    )

                ranked.sort(
                    key=lambda item: item[0],
                    reverse=True
                )

                if not ranked:
                    continue

                current_score = ranked[0][0]
                current_second = (
                    ranked[1][0]
                    if len(ranked) > 1
                    else 0.0
                )

                if current_score > best_score:

                    best_score = current_score
                    second_score = current_second
                    best_streams = ranked[0][1]

            if (
                best_score >= 0.93
                and best_score - second_score >= 0.08
            ):

                target_ids = list(best_streams)
                fuzzy_matches += 1

        # Um ID XMLTV pode alimentar vários canais Xtream.
        for sid in target_ids:
            add_mapping(source_id_map, source_id, sid)

    # --------------------------------------------------
    # MAPEAMENTO MANUAL
    # --------------------------------------------------

    manual_map, manual_matches = load_manual_mapping(
        channels,
        source_root
    )

    # O mapeamento manual substitui a associação automática
    # somente para os IDs que foram definidos manualmente.
    for xmltv_id, stream_ids in manual_map.items():

        source_id_map[xmltv_id] = list(stream_ids)

    # Remove IDs inexistentes por segurança.
    for xmltv_id in list(source_id_map.keys()):

        source_id_map[xmltv_id] = [
            sid
            for sid in source_id_map[xmltv_id]
            if sid in valid_streams
        ]

        if not source_id_map[xmltv_id]:
            del source_id_map[xmltv_id]

    # --------------------------------------------------
    # CRIAR XMLTV DE SAÍDA
    # --------------------------------------------------

    out = ET.Element(
        "tv",
        {
            "generator-info-name": "EPG Xtream Personalizado",
            "generator-info-url": (
                "https://github.com/"
                "sants2tech/epg-xtream-personalizado"
            )
        }
    )

    emitted = set()

    for ch in channels:

        sid = str(
            ch.get("stream_id", "")
        ).strip()

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

        logo = (
            ch.get("stream_icon") or ""
        ).strip()

        if logo.startswith(("http://", "https://")):

            ET.SubElement(
                node,
                "icon",
                {"src": logo}
            )

    # --------------------------------------------------
    # COPIAR PROGRAMAÇÃO PARA TODOS OS CANAIS ASSOCIADOS
    # --------------------------------------------------

    print(
        "Distribuindo a programação XMLTV...",
        flush=True
    )

    count = 0
    seen = set()

    programmes_per_channel = defaultdict(int)

    for programme in source_programmes:

        source_id = str(
            programme.get("channel", "")
        ).strip()

        target_ids = source_id_map.get(source_id, [])

        if not target_ids:
            continue

        # A mesma programação é replicada para cada canal
        # Xtream associado ao ID XMLTV de origem.
        for target_id in target_ids:

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

            programmes_per_channel[target_id] += 1

    print(
        f"Programas XMLTV distribuídos: {count}",
        flush=True
    )

    # --------------------------------------------------
    # FONTES XMLTV EXTERNAS: COMPLEMENTAR CANAIS SEM GUIA
    # --------------------------------------------------

    external_added = 0
    external_matches = 0
    external_source_stats = []

    external_sources = CONFIG.get("external_xmltv_sources", [])
    fuzzy_candidates = [
        (norm, stream_ids)
        for norm, stream_ids in name_to_channels.items()
        if len(norm) >= 5
    ]
    fuzzy_threshold = float(CONFIG.get("external_fuzzy_threshold", 0.96))

    for source in external_sources:
        source_name = str(source.get("name", "Fonte XMLTV")).strip()
        try:
            print(
                f"Consultando fonte complementar: {source_name}...",
                flush=True
            )
            external_root = fetch_external_xmltv(session, source)
            external_channels = external_root.findall("channel")
            external_programmes = external_root.findall("programme")

            external_map = {}
            for external_channel in external_channels:
                source_id = str(external_channel.get("id", "")).strip()
                if not source_id:
                    continue

                targets, method = match_external_channel(
                    external_channel,
                    id_to_channels,
                    name_to_channels,
                    fuzzy_candidates,
                    fuzzy_threshold
                )
                targets = [
                    sid for sid in targets
                    if sid in valid_streams
                    and programmes_per_channel.get(sid, 0) == 0
                ]

                if targets:
                    external_map[source_id] = targets
                    external_matches += len(targets)

            source_added = 0
            for programme in external_programmes:
                source_id = str(programme.get("channel", "")).strip()
                target_ids = external_map.get(source_id, [])

                if not target_ids:
                    continue

                for target_id in target_ids:
                    # A primeira fonte válida que preencher o canal tem prioridade.
                    if programmes_per_channel.get(target_id, 0) > 0:
                        continue

                    copied = ET.fromstring(
                        ET.tostring(programme, encoding="utf-8")
                    )
                    copied.set("channel", target_id)
                    key = programme_key(copied)

                    if key in seen:
                        continue

                    seen.add(key)
                    out.append(copied)
                    count += 1
                    external_added += 1
                    source_added += 1
                    programmes_per_channel[target_id] += 1

            external_source_stats.append(
                (source_name, len(external_map), source_added)
            )
            print(
                f"{source_name}: {len(external_map)} canais associados; "
                f"{source_added} programas adicionados.",
                flush=True
            )

        except Exception as exc:
            external_source_stats.append((source_name, 0, 0))
            print(
                f"Aviso: fonte complementar {source_name} indisponível: {exc}",
                flush=True
            )

    print(
        f"Programas adicionados por fontes externas: {external_added}",
        flush=True
    )

    # --------------------------------------------------
    # FALLBACK: EPG CURTO XTREAM
    # --------------------------------------------------

    fallback_count = 0

    if CONFIG.get("fallback_short_epg", True):

        pending = [
            ch
            for ch in channels
            if str(ch.get("stream_id", "")).strip()
            and programmes_per_channel.get(
                str(ch.get("stream_id", "")).strip(),
                0
            ) == 0
        ]

        max_channels = max(
            1,
            int(
                CONFIG.get(
                    "max_fallback_channels_per_run",
                    120
                )
            )
        )

        workers = max(
            1,
            min(
                int(CONFIG.get("fallback_workers", 6)),
                10
            )
        )

        # Alterna os canais consultados a cada dia.
        if pending:

            start_index = (
                datetime.now(timezone.utc).timetuple().tm_yday
                * max_channels
            ) % len(pending)

            ordered = (
                pending[start_index:]
                + pending[:start_index]
            )

            selected = ordered[:max_channels]

        else:
            selected = []

        total = len(selected)

        print(
            f"Fallback: {total} canais selecionados; "
            f"{workers} consultas simultâneas.",
            flush=True
        )

        with ThreadPoolExecutor(
            max_workers=workers
        ) as executor:

            futures = [
                executor.submit(
                    fetch_short_epg,
                    base,
                    username,
                    password,
                    str(ch.get("stream_id", "")).strip(),
                    CONFIG["max_programmes_per_channel"]
                )
                for ch in selected
            ]

            for done, future in enumerate(
                as_completed(futures),
                1
            ):

                sid, listings, error = future.result()

                if error:

                    print(
                        f"Aviso: EPG indisponível para "
                        f"{sid}: {error}",
                        flush=True
                    )

                for item in listings:

                    try:

                        start = item.get("start_timestamp")
                        stop = item.get("stop_timestamp")

                        if not start or not stop:
                            continue

                        start_dt = datetime.fromtimestamp(
                            int(start),
                            timezone.utc
                        )

                        stop_dt = datetime.fromtimestamp(
                            int(stop),
                            timezone.utc
                        )

                        programme = ET.Element(
                            "programme",
                            {
                                "start": start_dt.strftime(
                                    "%Y%m%d%H%M%S +0000"
                                ),
                                "stop": stop_dt.strftime(
                                    "%Y%m%d%H%M%S +0000"
                                ),
                                "channel": sid
                            }
                        )

                        add_text(
                            programme,
                            "title",
                            item.get("title")
                            or item.get("name")
                            or "Programação"
                        )

                        description = (
                            item.get("description")
                            or item.get("plot")
                            or ""
                        )

                        if description:

                            add_text(
                                programme,
                                "desc",
                                description
                            )

                        key = programme_key(programme)

                        if key not in seen:

                            seen.add(key)
                            out.append(programme)

                            count += 1
                            fallback_count += 1

                            programmes_per_channel[sid] += 1

                    except (
                        ValueError,
                        TypeError,
                        AttributeError
                    ):
                        continue

                if done % 10 == 0 or done == total:

                    print(
                        f"Progresso fallback: "
                        f"{done}/{total}",
                        flush=True
                    )

    # --------------------------------------------------
    # CONTINUIDADE: COMPLETAR INTERVALOS COM A ÚLTIMA EPG AINDA VÁLIDA
    # --------------------------------------------------

    carried_forward_count = 0

    if OUT.exists():
        try:
            previous_root = parse_xmltv(OUT.read_bytes())
            now_utc = datetime.now(timezone.utc)

            # Registra os intervalos já cobertos por dados novos.
            new_intervals = defaultdict(list)
            for current_programme in out.findall("programme"):
                channel_id = str(
                    current_programme.get("channel", "")
                ).strip()
                current_start = parse_xmltv_datetime(
                    current_programme.get("start")
                )
                current_stop = parse_xmltv_datetime(
                    current_programme.get("stop")
                )
                if channel_id and current_start and current_stop:
                    new_intervals[channel_id].append(
                        (current_start, current_stop)
                    )

            for previous_programme in previous_root.findall("programme"):
                target_id = str(
                    previous_programme.get("channel", "")
                ).strip()

                if not target_id or target_id not in valid_streams:
                    continue

                start_dt = parse_xmltv_datetime(
                    previous_programme.get("start")
                )
                stop_dt = parse_xmltv_datetime(
                    previous_programme.get("stop")
                )

                if not start_dt or not stop_dt:
                    continue

                # Mantém apenas a janela que o aplicativo ainda exibe.
                if (
                    stop_dt < now_utc - timedelta(hours=48)
                    or start_dt > now_utc + timedelta(days=7)
                ):
                    continue

                # Dados novos têm prioridade; o histórico só preenche lacunas.
                overlaps_new = any(
                    start_dt < current_stop and stop_dt > current_start
                    for current_start, current_stop
                    in new_intervals.get(target_id, [])
                )
                if overlaps_new:
                    continue

                copied = ET.fromstring(
                    ET.tostring(previous_programme, encoding="utf-8")
                )
                key = programme_key(copied)

                if key in seen:
                    continue

                seen.add(key)
                out.append(copied)
                count += 1
                carried_forward_count += 1
                programmes_per_channel[target_id] += 1

            print(
                f"Programas válidos reaproveitados da última EPG "
                f"para preencher lacunas: {carried_forward_count}",
                flush=True
            )

        except Exception as exc:
            print(
                f"Aviso: não foi possível reaproveitar a EPG anterior: {exc}",
                flush=True
            )

    # --------------------------------------------------
    # PROTEÇÃO: NÃO SUBSTITUIR UM EPG COM MAIOR COBERTURA
    # --------------------------------------------------

    if OUT.exists():
        try:
            previous_root = parse_xmltv(OUT.read_bytes())
            previous_programmes = previous_root.findall("programme")
            previous_channel_coverage = len({
                item.get("channel")
                for item in previous_programmes
                if item.get("channel")
            })
            new_channel_coverage = len({
                item.get("channel")
                for item in out.findall("programme")
                if item.get("channel")
            })

            if (
                previous_channel_coverage > 0
                and new_channel_coverage < previous_channel_coverage
                and count < len(previous_programmes)
            ):
                print(
                    "AVISO: a nova geração ficou abaixo da cobertura "
                    f"publicada ({new_channel_coverage} canais / {count} "
                    f"programas contra {previous_channel_coverage} canais / "
                    f"{len(previous_programmes)} programas). "
                    "O XMLTV e os relatórios anteriores foram preservados.",
                    flush=True
                )
                return

        except Exception as exc:
            print(
                f"Aviso: não foi possível comparar com o EPG anterior: {exc}",
                flush=True
            )

    # --------------------------------------------------
    # RELATÓRIO: CANAIS SEM EPG
    # --------------------------------------------------

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with (
        OUT.parent / "canais_sem_epg.csv"
    ).open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.writer(
            file,
            delimiter=";"
        )

        writer.writerow([
            "stream_id",
            "nome_xtream",
            "epg_channel_id",
            "programas"
        ])

        for ch in channels:

            sid = str(
                ch.get("stream_id", "")
            ).strip()

            if (
                sid
                and programmes_per_channel.get(sid, 0) == 0
            ):

                writer.writerow([
                    sid,
                    ch.get("name", ""),
                    ch.get("epg_channel_id", ""),
                    0
                ])

    # --------------------------------------------------
    # RELATÓRIO: IDS XMLTV DISPONÍVEIS
    # --------------------------------------------------

    with (
        OUT.parent / "canais_xmltv_disponiveis.csv"
    ).open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.writer(
            file,
            delimiter=";"
        )

        writer.writerow([
            "xmltv_id",
            "nome_fonte",
            "stream_ids_associados"
        ])

        for source_channel in source_channels:

            source_id = source_channel.get("id", "")

            names = [
                item.text or ""
                for item in source_channel.findall(
                    "display-name"
                )
            ]

            associated = source_id_map.get(
                source_id,
                []
            )

            writer.writerow([
                source_id,
                " / ".join(names),
                ", ".join(associated)
            ])

    # --------------------------------------------------
    # RELATÓRIO: CANAIS COM EPG
    # --------------------------------------------------

    with (
        OUT.parent / "canais_com_epg.csv"
    ).open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.writer(
            file,
            delimiter=";"
        )

        writer.writerow([
            "stream_id",
            "nome_xtream",
            "programas"
        ])

        for ch in channels:

            sid = str(
                ch.get("stream_id", "")
            ).strip()

            quantidade = programmes_per_channel.get(
                sid,
                0
            )

            if sid and quantidade > 0:

                writer.writerow([
                    sid,
                    ch.get("name", ""),
                    quantidade
                ])

    # --------------------------------------------------
    # VALIDAR RESULTADO
    # --------------------------------------------------

    if count == 0:

        raise RuntimeError(
            "Nenhum programa foi encontrado. "
            "O XMLTV anterior foi preservado; "
            "verifique se o provedor fornece EPG."
        )

    # --------------------------------------------------
    # GRAVAR XML COM SEGURANÇA
    # --------------------------------------------------

    tmp = OUT.with_suffix(".xml.tmp")

    ET.indent(
        out,
        space="  "
    )

    ET.ElementTree(out).write(
        tmp,
        encoding="utf-8",
        xml_declaration=True
    )

    tmp.replace(OUT)

    # --------------------------------------------------
    # ESTATÍSTICAS FINAIS
    # --------------------------------------------------

    channels_with_epg = len([
        sid
        for sid, quantity in programmes_per_channel.items()
        if quantity > 0
    ])

    channels_without_epg = (
        len(valid_streams) - channels_with_epg
    )

    mapped_ids = len(source_id_map)

    mapped_channels = len({
        sid
        for stream_ids in source_id_map.values()
        for sid in stream_ids
    })

    print("\n===================================")
    print("       RESULTADO DA ATUALIZAÇÃO")
    print("===================================")

    print(f"Total de canais: {len(valid_streams)}")

    print(
        f"Canais com programação: "
        f"{channels_with_epg}"
    )

    print(
        f"Canais sem programação: "
        f"{channels_without_epg}"
    )

    print(
        f"IDs XMLTV associados: {mapped_ids}"
    )

    print(
        f"Canais associados ao XMLTV: "
        f"{mapped_channels}"
    )

    print(f"Programas XMLTV: {count}")

    print(
        f"Programas obtidos por fontes externas: "
        f"{external_added}"
    )

    print(
        f"Programas reaproveitados da última EPG: "
        f"{carried_forward_count}"
    )

    for source_name, matched_channels, source_programmes in external_source_stats:
        print(
            f"Fonte {source_name}: {matched_channels} canais; "
            f"{source_programmes} programas"
        )

    print(
        f"Programas obtidos pelo fallback: "
        f"{fallback_count}"
    )

    print(f"Correspondências por ID: {id_matches}")

    print(
        f"Correspondências por nome exato: "
        f"{exact_matches}"
    )

    print(
        f"Correspondências aproximadas: "
        f"{fuzzy_matches}"
    )

    print(
        f"Correspondências manuais: "
        f"{manual_matches}"
    )

    print("-----------------------------------")

    print(
        f"Relatório sem EPG: "
        f"{OUT.parent / 'canais_sem_epg.csv'}"
    )

    print(
        f"Relatório XMLTV: "
        f"{OUT.parent / 'canais_xmltv_disponiveis.csv'}"
    )

    print(
        f"Relatório com EPG: "
        f"{OUT.parent / 'canais_com_epg.csv'}"
    )

    print(f"Arquivo gerado: {OUT}")

    print("===================================")
    print("       PROCESSO CONCLUÍDO")
    print("===================================")


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            f"ERRO: {exc}",
            file=sys.stderr
        )

        sys.exit(1)
