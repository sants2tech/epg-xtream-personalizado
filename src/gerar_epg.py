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

from concurrent.futures import ThreadPoolExecutor, as_completed
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
