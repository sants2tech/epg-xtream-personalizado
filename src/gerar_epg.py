
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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


def regional_aliases(value):
    """
    Gera chaves de afiliadas regionais sem misturar redes diferentes.
    Padroniza Record TV/RecordTV e remove apenas prefixos redundantes,
    preservando a marca Record, SBT ou Band e a localidade.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"\brecord\s*tv\b|\brecordtv\b", "record", text)
    text = re.sub(r"\binter\s*tv\b|\bintertv\b", "inter", text)
    text = re.sub(r"\b(globo|regional|regionais|tv|canal)\b", " ", text)

    geography = {
        "sp": "sao paulo", "rj": "rio de janeiro", "rio": "rio de janeiro",
        "rs": "rio grande do sul", "mg": "minas gerais",
        "pa": "para", "ba": "bahia", "sc": "santa catarina",
        "pr": "parana", "go": "goias", "df": "brasilia",
        "am": "amazonas", "es": "espirito santo",
        "ce": "ceara", "pb": "paraiba", "pe": "pernambuco",
        "ma": "maranhao", "pi": "piaui", "rn": "rio grande do norte"
    }
    text = " ".join(geography.get(word, word) for word in text.split())
    alias = re.sub(r"[^a-z0-9]+", "", text)
    original = normalize(value)
    if len(alias) >= 4 and alias != original:
        return [alias]
    return []

def channel_name_keys(value):
    keys = [normalize(value)]
    keys.extend(regional_aliases(value))
    return list(dict.fromkeys(key for key in keys if key))


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
        for norm in channel_name_keys(display_name):
            if norm in name_to_channels:
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
    """Consulta mais de uma ação Xtream antes de concluir que não há EPG."""

    errors = []
    headers = {
        "User-Agent": "EPG-Xtream-Personalizado/2.1",
        "Accept": "application/json"
    }

    # Alguns provedores preenchem get_simple_data_table mesmo quando
    # get_short_epg retorna uma lista vazia. Tentamos ambos sem misturar
    # respostas de canais diferentes.
    actions = (
        ("get_short_epg", {"limit": limit}),
        ("get_simple_data_table", {})
    )

    for action, extra_params in actions:
        try:
            params = {
                "username": username,
                "password": password,
                "action": action,
                "stream_id": sid,
                **extra_params
            }
            response = requests.get(
                f"{base}/player_api.php",
                params=params,
                timeout=(5, 15),
                headers=headers
            )
            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, list):
                listings = payload
            elif isinstance(payload, dict):
                listings = payload.get("epg_listings", [])
                if not listings and isinstance(payload.get("data"), list):
                    listings = payload["data"]
            else:
                listings = []

            if isinstance(listings, list) and listings:
                return sid, listings, None

        except (
            requests.RequestException,
            ValueError,
            TypeError
        ) as exc:
            errors.append(f"{action}: {exc}")

    detail = "; ".join(errors) if errors else (
        "get_short_epg e get_simple_data_table não retornaram programas"
    )
    return sid, [], detail



def fetch_stremio_channels(session):
    """Obtém a lista oficial de canais ao vivo do addon FrostView (Stremio)."""
    addon = "https://frostview.cloutteam.com"
    headers = {"User-Agent": "KamuflaTV-EPG/1.0", "Accept": "application/json"}
    response = session.get(f"{addon}/manifest.json", timeout=(15, 45), headers=headers)
    response.raise_for_status()
    manifest = response.json()

    # A FrostView publica canais como type=channel; aceitamos também tv
    # para manter compatibilidade caso o manifesto seja alterado.
    catalogs = [
        item for item in manifest.get("catalogs", [])
        if item.get("type") in ("channel", "tv") and item.get("id")
    ]
    if not catalogs:
        declared = ", ".join(manifest.get("types", []) or []) or "nenhum"
        raise RuntimeError(
            "O manifesto FrostView respondeu, mas não publicou catálogo de canais "
            f"(tipos declarados: {declared})."
        )

    channels_by_id = {}
    for catalog in catalogs:
        catalog_id = str(catalog["id"])
        catalog_type = str(catalog.get("type") or "channel")
        extras = catalog.get("extra", []) or []
        genre_extra = next((item for item in extras if item.get("name") == "genre"), None)
        genres = (genre_extra or {}).get("options") or [""]
        for genre in genres:
            skip = 0
            previous_first = None
            for page in range(100):
                parts = []
                if genre:
                    parts.append("genre=" + requests.utils.quote(str(genre), safe=""))
                if skip:
                    parts.append("skip=" + str(skip))
                suffix = "/" + "&".join(parts) if parts else ""
                url = (
                    f"{addon}/catalog/{catalog_type}/"
                    f"{requests.utils.quote(catalog_id, safe='')}{suffix}.json"
                )
                result = session.get(url, timeout=(15, 45), headers=headers)
                result.raise_for_status()
                payload = result.json()
                metas = payload.get("metas", []) if isinstance(payload, dict) else []
                if not metas:
                    break
                first_id = str(metas[0].get("id", ""))
                if not first_id or first_id == previous_first:
                    break
                previous_first = first_id
                for meta in metas:
                    channel_id = str(meta.get("id", "")).strip()
                    name = str(meta.get("name", "")).strip()
                    if channel_id and name:
                        channels_by_id[channel_id] = {
                            "stream_id": channel_id,
                            "name": name,
                            "stream_icon": meta.get("logo") or meta.get("poster") or "",
                            "epg_channel_id": "",
                            "custom_sid": ""
                        }
                skip += len(metas)

    channels = list(channels_by_id.values())
    if not channels:
        raise RuntimeError(
            "A FrostView respondeu ao manifesto, mas não retornou canais no catálogo."
        )
    print(f"Addon FrostView: {len(channels)} canais importados.", flush=True)
    return channels


def fetch_xtream_live_channels(session, base, username, password):
    """Obtém os IDs numéricos reais usados pelo player KamuflaTV via API Xtream."""
    response = xtream_get(
        session, base, username, password,
        action="get_live_streams"
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(
            "A API Xtream não retornou uma lista válida em get_live_streams."
        )

    channels_by_id = {}
    for item in payload:
        sid = str(item.get("stream_id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not sid or not name:
            continue
        channels_by_id[sid] = {
            "stream_id": sid,
            "name": name,
            "stream_icon": item.get("stream_icon") or "",
            "epg_channel_id": item.get("epg_channel_id") or "",
            "custom_sid": item.get("custom_sid") or ""
        }

    if not channels_by_id:
        raise RuntimeError(
            "A API Xtream respondeu, mas não retornou canais ao vivo válidos."
        )
    return list(channels_by_id.values())


def main():

    print("Iniciando EPG exclusivamente para os canais do catálogo Stremio...", flush=True)

    session = requests.Session()
    retry_policy = Retry(
        total=6,
        connect=6,
        read=4,
        status=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "KamuflaTV-EPG/2.0",
        "Accept": "application/json, application/xml, text/xml, */*"
    })

    # A lista Stremio é a única fonte de identidade dos canais.
    # Não consulta Xtream, credenciais, XMLTV do provedor nem IDs numéricos.
    channels = fetch_stremio_channels(session)
    valid_streams = {
        str(channel.get("stream_id", "")).strip()
        for channel in channels
        if str(channel.get("stream_id", "")).strip()
    }
    channels = [
        channel for channel in channels
        if str(channel.get("stream_id", "")).strip() in valid_streams
    ]

    print(f"Canais encontrados no catálogo Stremio: {len(channels)}", flush=True)

    id_to_channels = defaultdict(list)
    name_to_channels = defaultdict(list)
    for channel in channels:
        channel_id = str(channel["stream_id"]).strip()
        for key in channel_name_keys(channel.get("name", "")):
            if channel_id not in name_to_channels[key]:
                name_to_channels[key].append(channel_id)

    fuzzy_candidates = [
        (key, ids) for key, ids in name_to_channels.items()
        if len(key) >= 5
    ]

    # A programação vem apenas das fontes XMLTV públicas configuradas abaixo.
    source_channels = []
    source_programmes = []
    source_root = ET.Element("tv")
    source_id_map = {}
    id_matches = 0
    exact_matches = 0
    fuzzy_matches = 0
    manual_map, manual_matches = load_manual_mapping(channels, source_root)

    out = ET.Element(
        "tv",
        {
            "generator-info-name": "KamuflaTV EPG Stremio",
            "generator-info-url": "https://github.com/sants2tech/epg-xtream-personalizado"
        }
    )

    for channel in channels:
        channel_id = str(channel["stream_id"]).strip()
        node = ET.SubElement(out, "channel", {"id": channel_id})
        add_text(node, "display-name", channel.get("name") or channel_id)
        logo = str(channel.get("stream_icon") or "").strip()
        if logo.startswith(("http://", "https://")):
            ET.SubElement(node, "icon", {"src": logo})

    count = 0
    seen = set()
    programmes_per_channel = defaultdict(int)

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

            # Copia todos os programas da mesma fonte para cada canal associado.
            # A prioridade entre fontes é definida ao montar external_map:
            # só entram canais que ainda não receberam dados de fontes anteriores.
            source_added = 0
            for programme in external_programmes:
                source_id = str(programme.get("channel", "")).strip()
                target_ids = external_map.get(source_id, [])

                if not target_ids:
                    continue

                for target_id in target_ids:
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
    # --------------------------------------------------
    # FALLBACK XTREAM DESATIVADO: os IDs agora são Stremio.
    # --------------------------------------------------
    fallback_count = 0

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
    # Não criar programação fictícia. Canais sem correspondência real
    # permanecem sem grade e são listados no relatório.
    real_programmes_per_channel = dict(programmes_per_channel)
    continuous_channels = set()
    continuous_added = 0
    channels_without_real_epg = sum(
        1 for ch in channels
        if programmes_per_channel.get(str(ch.get("stream_id", "")), 0) == 0
    )
    print(f"Canais sem programação real: {channels_without_real_epg}", flush=True)

    # PROTEÇÃO: NÃO SUBSTITUIR UM EPG COM MAIOR COBERTURA
    # --------------------------------------------------

    if OUT.exists():
        try:
            previous_root = parse_xmltv(OUT.read_bytes())
            previous_programmes = previous_root.findall("programme")
            previous_ids = {
                item.get("channel") for item in previous_programmes
                if item.get("channel") in valid_streams
            }
            previous_channel_coverage = len(previous_ids)
            previous_relevant_count = sum(
                1 for item in previous_programmes
                if item.get("channel") in valid_streams
            )
            new_channel_coverage = len({
                item.get("channel")
                for item in out.findall("programme")
                if item.get("channel") in valid_streams
            })
            if (
                previous_channel_coverage > 0
                and new_channel_coverage < previous_channel_coverage
                and count < previous_relevant_count
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
            "nome_stremio",
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
    # RELATÓRIO: CANAIS SEM GRADE REAL (RECEBEM CONTINUIDADE)
    # --------------------------------------------------

    with (OUT.parent / "canais_sem_grade_real.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["stream_id", "nome_canal", "programacao_publicada"])
        for ch in channels:
            sid = str(ch.get("stream_id", "")).strip()
            if sid and sid in continuous_channels:
                writer.writerow([sid, ch.get("name", ""), "Programação contínua"])

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
            "nome_canal",
            "programas",
            "tipo_programacao"
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
                    quantidade,
                    "Programação contínua" if sid in continuous_channels else "EPG de fonte"
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
    # ÍNDICE JSON DA EPG EXCLUSIVA DO CATÁLOGO STREMIO.
    # --------------------------------------------------

    all_channel_ids = {
        str(sid).strip()
        for sid in valid_streams
        if str(sid).strip()
    }

    epg_index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel_names": {},
        "channels": {}
    }

    for channel_node in out.findall("channel"):
        channel_id = str(channel_node.get("id", "")).strip()
        if channel_id not in all_channel_ids:
            continue

        channel_name = (
            channel_node.findtext("display-name")
            or channel_id
        )
        epg_index["channel_names"][channel_id] = channel_name

    indexed_programme_count = 0

    for programme in out.findall("programme"):
        channel_id = str(programme.get("channel", "")).strip()
        if channel_id not in all_channel_ids:
            continue

        entry = {
            "start": programme.get("start", ""),
            "stop": programme.get("stop", ""),
            "title": programme.findtext("title") or "Programa",
            "desc": programme.findtext("desc") or ""
        }

        category = programme.findtext("category")
        if category:
            entry["category"] = category

        epg_index["channels"].setdefault(channel_id, []).append(entry)
        indexed_programme_count += 1

    json_path = OUT.parent / "epg-stremio.json"
    json_tmp = json_path.with_suffix(".json.tmp")

    json_tmp.write_text(
        json.dumps(
            epg_index,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )
    json_tmp.replace(json_path)

    print(
        f"Índice EPG Stremio: {len(epg_index['channels'])} canais; "
        f"{indexed_programme_count} programas; arquivo: {json_path}",
        flush=True
    )

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

    print(f"Total de canais Stremio: {len(valid_streams)}")

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
        f"Programas obtidos pelo fallback Xtream: "
        f"{fallback_count}"
    )
    print(
        f"Canais com programação contínua: {len(continuous_channels)}"
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
