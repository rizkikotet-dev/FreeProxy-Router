#!/usr/bin/env python3
"""
main.py v6.3

AI Proxy Finder / Validator
===========================

Fitur v6.3:
- Multi-endpoint: test proxy di Responses API DAN Chat Completions.
  Proxy dianggap aktif jika salah satu endpoint mengembalikan output AI.
- Schema-aware AI response validation.
- HTTP / HTTPS / SOCKS4 / SOCKS5.
- Auto fallback HTTP proxy -> HTTPS proxy pada port 443.
- Double-check AI request.
- Deteksi "incomplete" response.
- Reject metadata / status / echo prompt.
- SSE / NDJSON support.
- Multi-source proxy (Proxifly, Monosans, TheSpeedX, IPLocate).
- --probe untuk debugging satu proxy.
- Rich dashboard, JSON export, Omniroute export.
- Graceful Ctrl+C.

Install:
    pip install requests rich
    pip install "requests[socks]"

Contoh:
    python main.py --all --api-key "KEY"
    python main.py --all --all-sources --api-key "KEY"
    python main.py --probe http://1.2.3.4:8080 --api-key "KEY"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import socket
import sys
import time

from collections import Counter
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.syntax import Syntax


# ============================================================================
# APP CONFIG
# ============================================================================

__version__ = "6.3"

console = Console()
log = logging.getLogger("proxy_finder")
_shutdown = Event()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Default targets — multi-endpoint
# ---------------------------------------------------------------------------

DEFAULT_TEST_URLS: list[str] = [
    "https://opencode.ai/zen/v1/responses",
    "https://opencode.ai/zen/v1/chat/completions",
]

DEFAULT_MODEL = "big-pickle"

DEFAULT_PING_PROMPT = "Reply with exactly: PONG"

# ---------------------------------------------------------------------------
# Timeouts / workers
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT = 3
DEFAULT_MAX_TIME = 15
SOURCE_TIMEOUT = 30

DEFAULT_WORKERS = 10
DEFAULT_BATCH_MULTIPLIER = 2

DEFAULT_SKIP_PORT_CHECK = True

PROTOCOL_SCHEME = {
    "http": "http",
    "https": "https",
    "socks4": "socks4",
    "socks5": "socks5",
}

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

PROXIFLY_BASE = (
    "https://raw.githubusercontent.com/"
    "proxifly/free-proxy-list/main/proxies"
)

MONOSANS_BASE = (
    "https://raw.githubusercontent.com/"
    "monosans/proxy-list/refs/heads/main/proxies"
)

THESPEEDX_BASE = (
    "https://raw.githubusercontent.com/"
    "TheSpeedX/SOCKS-List/master"
)

IPLOCATE_BASE = (
    "https://raw.githubusercontent.com/"
    "iplocate/free-proxy-list/main/protocols"
)

IPLOCATE_ALL = [
    f"{IPLOCATE_BASE}/http.txt",
    f"{IPLOCATE_BASE}/https.txt",
    f"{IPLOCATE_BASE}/socks4.txt",
    f"{IPLOCATE_BASE}/socks5.txt",
]

SOURCE_PRESETS: dict[str, Any] = {
    "proxifly-all": f"{PROXIFLY_BASE}/all/data.json",
    "proxifly-http": f"{PROXIFLY_BASE}/protocols/http/data.json",
    "proxifly-socks4": f"{PROXIFLY_BASE}/protocols/socks4/data.json",
    "proxifly-socks5": f"{PROXIFLY_BASE}/protocols/socks5/data.json",
    "monosans": f"{MONOSANS_BASE}.json",
    "monosans-json": f"{MONOSANS_BASE}.json",
    "monosans-http": f"{MONOSANS_BASE}/http.txt",
    "monosans-socks4": f"{MONOSANS_BASE}/socks4.txt",
    "monosans-socks5": f"{MONOSANS_BASE}/socks5.txt",
    "thespeedx-http": f"{THESPEEDX_BASE}/http.txt",
    "thespeedx-socks4": f"{THESPEEDX_BASE}/socks4.txt",
    "thespeedx-socks5": f"{THESPEEDX_BASE}/socks5.txt",
    "iplocate": list(IPLOCATE_ALL),
    "iplocate-all": list(IPLOCATE_ALL),
    "iplocate-http": f"{IPLOCATE_BASE}/http.txt",
    "iplocate-https": f"{IPLOCATE_BASE}/https.txt",
    "iplocate-socks4": f"{IPLOCATE_BASE}/socks4.txt",
    "iplocate-socks5": f"{IPLOCATE_BASE}/socks5.txt",
}


# ============================================================================
# UI STATUS
# ============================================================================

STAGE_LABEL = {
    "port_closed": ("PORT", "red"),
    "unsupported": ("UNSUPPORTED", "yellow"),
    "test_failed": ("REQUEST", "red"),
    "rate_limited": ("429", "yellow"),
    "region_blocked": ("403 GEO", "bold magenta"),
    "auth_required": ("407", "yellow"),
    "api_error": ("API", "red"),
    "incomplete": ("INCOMPLETE", "bold yellow"),
    "invalid_ai_response": ("INVALID_AI", "bold red"),
    "success": ("OK", "bold green"),
    "skipped": ("SKIP", "dim"),
}


def _on_sigint(signum, frame):  # noqa: ARG001
    if _shutdown.is_set():
        console.print("\n[bold red]Force exit.[/]")
        raise SystemExit(130)
    _shutdown.set()
    console.print(
        "\n[yellow]Ctrl+C diterima — "
        "menghentikan scan secara aman...[/]"
    )


signal.signal(signal.SIGINT, _on_sigint)


# ============================================================================
# MODELS
# ============================================================================


@dataclass(slots=True)
class ProxyEntry:
    raw: str
    protocol: str
    ip: str
    port: int
    country: str = ""
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def has_auth(self) -> bool:
        return self.username is not None

    @property
    def authority(self) -> str:
        host = self.ip
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"

    @property
    def curl_proxy(self) -> Optional[str]:
        scheme = PROTOCOL_SCHEME.get(self.protocol)
        if not scheme:
            return None
        auth = ""
        if self.username is not None:
            auth = (
                f"{quote(self.username, safe='')}:"
                f"{quote(self.password or '', safe='')}@"
            )
        return f"{scheme}://{auth}{self.authority}"

    def display_proxy(self, show_auth: bool = False) -> str:
        scheme = PROTOCOL_SCHEME.get(self.protocol, self.protocol)
        host = self.ip
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        auth = ""
        if self.username is not None:
            if show_auth:
                auth = f"{self.username}:{self.password or ''}@"
            else:
                auth = f"{self.username}:••••@"
        return f"{scheme}://{auth}{host}:{self.port}"


@dataclass(slots=True)
class CheckResult:
    entry: ProxyEntry
    ok: bool
    stage: str
    detail: str = ""
    latency_ms: float = 0.0
    ai_text: str = ""
    proxy_url: str = ""
    endpoint_used: str = ""


# ============================================================================
# GENERIC HTTP SESSION
# ============================================================================


def make_session(
    retries: int = 2,
    backoff: float = 0.35,
) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/json, "
            "text/event-stream;q=0.9, "
            "application/x-ndjson;q=0.8, "
            "text/plain;q=0.5, "
            "*/*;q=0.2"
        ),
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=30,
        pool_maxsize=30,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_source_session: Optional[requests.Session] = None


def get_source_session() -> requests.Session:
    global _source_session
    if _source_session is None:
        _source_session = make_session()
    return _source_session


# ============================================================================
# STRING HELPERS
# ============================================================================


def short(value: Any, width: int = 70) -> str:
    value = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs:02d}s"


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = (
        value
        .replace("\x00", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if not value:
        return ""
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def normalize_compact(value: str) -> str:
    return re.sub(
        r"\s+", " ", normalize_text(value),
    ).strip().lower()


def endpoint_label(url: str) -> str:
    """Short label for an endpoint URL."""
    lower = url.lower()
    if "/responses" in lower:
        return "responses"
    if "/chat/completions" in lower:
        return "chat"
    parsed = urlparse(url)
    return parsed.path.strip("/") or parsed.hostname or url


# ============================================================================
# PROXY PARSING
# ============================================================================

_PROTO_RE = re.compile(r"^(https?|socks4a?|socks5)://", re.I)


def infer_protocol(source: str) -> str:
    path = (
        urlparse(source).path
        if "://" in source
        else source.replace("\\", "/")
    )
    name = unquote(path.rsplit("/", 1)[-1].lower())
    for tag in ("socks5", "socks4", "https", "http"):
        if tag in name:
            return tag
    return ""


def extract_country(item: dict[str, Any]) -> str:
    for key in (
        "country", "country_code", "countryCode", "geo", "geolocation",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            return value.upper() if len(value) <= 3 else value[:24]
    nested = item.get("ip_data")
    if isinstance(nested, dict):
        for key in ("country_code", "countryCode", "country"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                value = value.strip()
                return value.upper() if len(value) <= 3 else value[:24]
    return ""


def parse_authority(
    value: str,
) -> Optional[tuple[str, int, Optional[str], Optional[str]]]:
    value = value.strip()
    if not value:
        return None
    if "://" in value:
        value = value.split("://", 1)[1]
    username = None
    password = None
    if "@" in value:
        creds, value = value.rsplit("@", 1)
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
    if value.startswith("["):
        end = value.find("]")
        if end <= 0 or len(value) <= end + 2 or value[end + 1] != ":":
            return None
        host = value[1:end]
        port_s = value[end + 2:]
    else:
        host, sep, port_s = value.rpartition(":")
        if not sep:
            return None
    if not host.strip():
        return None
    if not port_s.strip().isdigit():
        return None
    port = int(port_s.strip())
    if not 1 <= port <= 65535:
        return None
    return (host.strip(), port, username, password)


def build_entry(
    value: str,
    default_proto: str,
) -> Optional[ProxyEntry]:
    value = value.strip()
    if not value:
        return None
    proto = default_proto
    match = _PROTO_RE.match(value)
    if match:
        proto = match.group(1).lower().replace("socks4a", "socks4")
    if proto not in PROTOCOL_SCHEME:
        return None
    parsed = parse_authority(value)
    if not parsed:
        return None
    ip, port, user, password = parsed
    return ProxyEntry(
        value, proto, ip, port,
        username=user, password=password,
    )


def parse_json(
    raw_data: Any,
    force_proto: str = "",
) -> list[ProxyEntry]:
    if not isinstance(raw_data, list):
        return []
    out: list[ProxyEntry] = []
    for item in raw_data:
        if not isinstance(item, dict):
            continue
        ip = str(
            item.get("ip")
            or item.get("host")
            or item.get("addr")
            or item.get("address")
            or item.get("ipv4")
            or ""
        ).strip()
        if ip.startswith("[") and ip.endswith("]"):
            ip = ip[1:-1]
        try:
            port = int(str(item.get("port")).strip())
        except (TypeError, ValueError):
            continue
        proto_raw = (
            item.get("protocol")
            or item.get("proto")
            or item.get("scheme")
            or item.get("type")
            or ""
        )
        if isinstance(proto_raw, list):
            proto_raw = next(
                (
                    p for p in proto_raw
                    if str(p).lower().strip() in PROTOCOL_SCHEME
                ),
                "",
            )
        proto = (
            str(proto_raw).lower().strip().replace("socks4a", "socks4")
            or force_proto
        )
        if (
            proto not in PROTOCOL_SCHEME
            or not ip
            or not 1 <= port <= 65535
        ):
            continue
        user = (
            item.get("username")
            or item.get("user")
            or item.get("auth_username")
        )
        pwd = (
            item.get("password")
            or item.get("pass")
            or item.get("auth_password")
        )
        out.append(
            ProxyEntry(
                f"{proto}://{ip}:{port}",
                proto, ip, port,
                extract_country(item),
                str(user) if user is not None else None,
                str(pwd) if pwd is not None else None,
            )
        )
    return out


def parse_txt(
    text: str,
    default_proto: str,
) -> list[ProxyEntry]:
    out: list[ProxyEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts_ws = line.split()
        if not parts_ws:
            continue
        line = parts_ws[0]
        entry = build_entry(line, default_proto)
        if entry:
            out.append(entry)
            continue
        parts = line.split(":")
        if len(parts) >= 4 and parts[0].count(".") == 3:
            candidate = f"{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            entry = build_entry(candidate, default_proto)
            if entry:
                out.append(entry)
    return out


def dedupe(entries: list[ProxyEntry]) -> list[ProxyEntry]:
    best: dict[tuple[str, str, int], ProxyEntry] = {}
    for entry in entries:
        key = (entry.protocol, entry.ip.lower(), entry.port)
        prev = best.get(key)
        if prev is None:
            best[key] = entry
            continue
        if not prev.has_auth and entry.has_auth:
            best[key] = entry
    return list(best.values())


def is_inline_proxy(value: str) -> bool:
    s = value.strip()
    if s.startswith(("http://", "https://")) and "/" in s[8:]:
        return False
    return parse_authority(s) is not None


# ============================================================================
# SOURCE LOADING
# ============================================================================


def fetch_text(source: str, timeout: int) -> str:
    if source.startswith(("http://", "https://")):
        response = get_source_session().get(source, timeout=timeout)
        response.raise_for_status()
        return response.text.lstrip("\ufeff")
    path = Path(source)
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"Tidak bisa decode file: {source}")


def expand_github_tree(url: str) -> list[str]:
    m = re.match(
        r"https?://github\.com/"
        r"([^/]+)/([^/]+)/tree/"
        r"([^/]+)/?(.*)$",
        url.strip(),
    )
    if not m:
        return [url]
    owner, repo, branch, path = m.groups()
    queue = [path.strip("/")]
    files: list[str] = []
    while queue:
        current = queue.pop(0)
        api = (
            f"https://api.github.com/repos/"
            f"{owner}/{repo}/contents/"
            f"{quote(current, safe='/')}"
        )
        page = 1
        while True:
            response = get_source_session().get(
                api,
                params={"ref": branch, "per_page": 100, "page": page},
                headers={"Accept": "application/vnd.github+json"},
                timeout=20,
            )
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list):
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "dir":
                    queue.append(str(item.get("path", "")))
                elif (
                    item.get("type") == "file"
                    and str(item.get("name", ""))
                    .lower()
                    .endswith((".txt", ".json"))
                    and item.get("download_url")
                ):
                    files.append(str(item["download_url"]))
            if len(items) < 100:
                break
            page += 1
    if not files:
        raise ValueError(f"Tidak ada file .txt/.json di {url}")
    return sorted(set(files))


def resolve_sources(
    source_arg: Optional[str],
    json_url: Optional[str],
    all_sources: bool,
) -> list[str]:
    if all_sources:
        selected: list[str] = []
        for value in SOURCE_PRESETS.values():
            if isinstance(value, str):
                selected.append(value)
            else:
                selected.extend(value)
        return list(dict.fromkeys(selected))
    if source_arg:
        value = SOURCE_PRESETS[source_arg]
        if isinstance(value, str):
            return [value]
        return list(value)
    urls = [
        item.strip()
        for item in (json_url or "").split(",")
        if item.strip()
    ]
    if urls:
        return urls
    return [f"{PROXIFLY_BASE}/all/data.json"]


def src_label(source: str) -> str:
    if source.startswith(("http://", "https://")):
        parts = [
            p for p in urlparse(source).path.split("/") if p
        ]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        if parts:
            return parts[-1]
    return source.replace("\\", "/").rsplit("/", 1)[-1]


def parse_source(
    raw: str,
    source: str,
    force_proto: str,
) -> list[ProxyEntry]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return parse_json(data, force_proto)
    return parse_txt(
        raw, force_proto or infer_protocol(source) or "http",
    )


def load_entries(
    sources: list[str],
    force_proto: str,
    timeout: int,
) -> tuple[list[ProxyEntry], dict[str, int], list[str]]:
    all_entries: list[ProxyEntry] = []
    per_src: dict[str, int] = {}
    errors: list[str] = []
    expanded: list[str] = []
    for source in sources:
        if is_inline_proxy(source):
            entry = build_entry(source, force_proto or "http")
            per_src[source[:40]] = int(entry is not None)
            if entry:
                all_entries.append(entry)
            continue
        try:
            expanded.extend(expand_github_tree(source))
        except Exception as exc:
            errors.append(f"{src_label(source)} -> {exc}")
    for source in expanded:
        label = src_label(source)
        try:
            raw = fetch_text(source, timeout)
            entries = parse_source(raw, source, force_proto)
            per_src[label] = per_src.get(label, 0) + len(entries)
            all_entries.extend(entries)
        except Exception as exc:
            errors.append(f"{label} -> {exc}")
    return (dedupe(all_entries), per_src, errors)


# ============================================================================
# SOCKS DEPENDENCY
# ============================================================================


def socks_ok(entries: list[ProxyEntry]) -> bool:
    if not any(e.protocol in ("socks4", "socks5") for e in entries):
        return True
    try:
        import socks  # noqa: F401
        return True
    except ImportError:
        return False


# ============================================================================
# AI PAYLOAD
# ============================================================================


def is_responses_api(url: str) -> bool:
    return "/responses" in url.lower()


def build_ai_payload(
    url: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    if is_responses_api(url):
        return {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_tokens,
            "stream": False,
            "temperature": 0.7,
            "top_p": 0.95,
        }
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "stream": False,
        "temperature": 0.7,
        "top_p": 0.95,
        "chat_template_kwargs": {"thinking": False},
    }


# ============================================================================
# AI VALIDATION
# ============================================================================

_BAD_EXACT = {
    "ok", "success", "accepted", "queued", "processing",
    "completed", "ack", "acknowledged", "done",
    "true", "false", "null", "none",
}

_ERROR_PATTERNS = re.compile(
    r"^(error|err\b|exception|failed|failure|invalid|"
    r"not found|unauthorized|forbidden|rate.?limit|"
    r"quota exceeded|model not found|api key|"
    r"authentication|access denied|internal server error|"
    r"bad request|method not allowed|service unavailable|"
    r"gateway timeout|not implemented|insufficient.?quota|"
    r"billing|payment required|"
    r"the model .* (does not|is not|cannot|was not))",
    re.IGNORECASE,
)

_ID_PATTERNS = re.compile(
    r"^(resp_|chatcmpl-|req_|msg_|cmpl-|ft-|run_|"
    r"asst_|thread_|"
    r"msg-[a-zA-Z0-9]{20,}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-)",
)


def valid_candidate(text: str, prompt_norm: str) -> bool:
    text = normalize_text(text)
    if not text:
        return False
    compact = normalize_compact(text)
    if not compact:
        return False
    if compact in _BAD_EXACT:
        return False
    if prompt_norm and compact == prompt_norm:
        return False
    if _ERROR_PATTERNS.match(compact):
        return False
    if _ID_PATTERNS.match(text):
        return False
    if len(compact) == 1 and not compact.isalpha():
        return False
    return True


def extract_content_text(value: Any) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            text = extract_content_text(item)
            if text:
                chunks.append(text)
        return "\n".join(chunks).strip()
    if isinstance(value, dict):
        for key in ("text", "output_text", "value"):
            val = value.get(key)
            if isinstance(val, str) and val.strip():
                return normalize_text(val)
        for key in ("content", "parts", "delta"):
            if key in value:
                result = extract_content_text(value[key])
                if result:
                    return result
    return ""


def extract_chat_completions(data: Any) -> list[str]:
    results: list[str] = []
    if not isinstance(data, dict):
        return results
    choices = data.get("choices")
    if not isinstance(choices, list):
        return results
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            role = str(message.get("role", "")).lower()
            content = message.get("content")
            if role in ("assistant", "model", "bot", ""):
                text = extract_content_text(content)
                if text:
                    results.append(text)
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            text = extract_content_text(content)
            if text:
                results.append(text)
        text = choice.get("text")
        if isinstance(text, str):
            text = normalize_text(text)
            if text:
                results.append(text)
    return results


def extract_responses_api(data: Any) -> list[str]:
    results: list[str] = []
    if not isinstance(data, dict):
        return results
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        output_text = normalize_text(output_text)
        if output_text:
            results.append(output_text)
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).lower()
            if item_type in ("message", ""):
                content = item.get("content")
                text = extract_content_text(content)
                if text:
                    results.append(text)
            for key in ("text", "output_text", "content"):
                if key not in item:
                    continue
                text = extract_content_text(item[key])
                if text:
                    results.append(text)
    return results


def extract_ai_candidates(
    data: Any,
    responses_api: bool,
) -> list[str]:
    if responses_api:
        results = extract_responses_api(data)
        if results:
            return results
        return extract_chat_completions(data)
    results = extract_chat_completions(data)
    if results:
        return results
    return extract_responses_api(data)


def _parse_stream(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("event:"):
            continue
        if line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _merge_stream_text(
    events: list[dict[str, Any]],
    responses_api: bool,
    prompt: str,
) -> str:
    chunks: list[str] = []
    prompt_norm = normalize_compact(prompt)
    for event in events:
        candidates = extract_ai_candidates(event, responses_api)
        for candidate in candidates:
            candidate = normalize_text(candidate)
            if candidate and valid_candidate(candidate, prompt_norm):
                chunks.append(candidate)
    if not chunks:
        return ""
    result = ""
    for chunk in chunks:
        if not result:
            result = chunk
            continue
        if chunk == result:
            continue
        if chunk.startswith(result):
            result = chunk
            continue
        if result.startswith(chunk):
            continue
        result += chunk
    return normalize_text(result)


def extract_ai_text_from_response(
    response: requests.Response,
    prompt: str,
    endpoint_url: str,
) -> str:
    content_type = (
        response.headers.get("Content-Type", "").lower()
    )
    raw = response.text or ""
    responses_api = is_responses_api(endpoint_url)

    if (
        "text/event-stream" in content_type
        or "application/x-ndjson" in content_type
    ):
        return _merge_stream_text(
            _parse_stream(raw), responses_api, prompt,
        )

    try:
        data = response.json()
    except ValueError:
        text = normalize_text(raw)
        if text and valid_candidate(text, normalize_compact(prompt)):
            return text
        return ""

    prompt_norm = normalize_compact(prompt)
    candidates = extract_ai_candidates(data, responses_api)
    valid = [
        normalize_text(text)
        for text in candidates
        if valid_candidate(text, prompt_norm)
    ]
    if not valid:
        return ""
    return max(valid, key=len)


def extract_api_error(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        msg = error.get("message") or ""
        code = error.get("type") or error.get("code") or "error"
        if msg:
            return f"{code}: {msg}"
        return str(code)
    if isinstance(error, str):
        return error
    for key in ("message", "detail", "error_message"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if any(
            key_name in data
            for key_name in ("output", "output_text", "choices")
        ):
            return None
        return value.strip()
    return None


def check_incomplete(data: dict[str, Any]) -> Optional[str]:
    resp_status = str(data.get("status", "")).lower()
    if resp_status != "incomplete":
        return None
    reason = "unknown"
    incomplete = data.get("incomplete_details")
    if isinstance(incomplete, dict):
        reason = incomplete.get("reason", "unknown")
    usage = data.get("usage")
    token_info = ""
    if isinstance(usage, dict):
        out_tok = usage.get("output_tokens", "?")
        in_tok = usage.get("input_tokens", "?")
        token_info = f" (in={in_tok} out={out_tok})"
    return (
        f"Response incomplete: {reason}{token_info}. "
        f"Coba tingkatkan --max-tokens"
    )


def validate_ai_response(
    response: requests.Response,
    prompt: str,
    endpoint_url: str,
) -> tuple[bool, str, str, str]:
    """Return (ok, stage, detail, ai_text)."""
    if not (200 <= response.status_code < 300):
        return (False, "api_error", f"HTTP {response.status_code}", "")

    raw = response.text or ""

    try:
        data = response.json()
    except ValueError:
        data = None

    if data is not None and isinstance(data, dict):
        api_error = extract_api_error(data)
        if api_error:
            return (
                False,
                "api_error",
                "API error (HTTP 2xx): " + short(api_error, 180),
                "",
            )
        incomplete_detail = check_incomplete(data)
        if incomplete_detail:
            return (False, "incomplete", incomplete_detail, "")

    ai_text = extract_ai_text_from_response(
        response, prompt, endpoint_url,
    )

    if not ai_text:
        snippet = (
            raw[:300].replace("\n", " ").replace("\r", "").strip()
        )
        if snippet:
            return (
                False,
                "invalid_ai_response",
                "HTTP 2xx — no valid AI output "
                f"| body: {short(snippet, 180)}",
                "",
            )
        return (
            False,
            "invalid_ai_response",
            "HTTP 2xx — empty response body",
            "",
        )

    prompt_norm = normalize_compact(prompt)
    response_norm = normalize_compact(ai_text)

    if prompt_norm and response_norm == prompt_norm:
        return (
            False,
            "invalid_ai_response",
            "Response hanya meng-echo prompt",
            "",
        )

    return (
        True,
        "success",
        f'AI: "{short(ai_text, 180)}"',
        ai_text,
    )


# ============================================================================
# PROXY URL VARIANTS
# ============================================================================


def build_proxy_url(entry: ProxyEntry, scheme: str) -> str:
    host = entry.ip
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if entry.username is not None:
        auth = (
            f"{quote(entry.username, safe='')}:"
            f"{quote(entry.password or '', safe='')}@"
        )
    return f"{scheme}://{auth}{host}:{entry.port}"


def proxy_variants(entry: ProxyEntry) -> list[str]:
    variants: list[str] = []

    def add(scheme: str) -> None:
        url = build_proxy_url(entry, scheme)
        if url not in variants:
            variants.append(url)

    if entry.protocol == "http":
        add("http")
        if entry.port == 443:
            add("https")
    elif entry.protocol == "https":
        add("https")
        add("http")
    elif entry.protocol == "socks4":
        add("socks4")
    elif entry.protocol == "socks5":
        add("socks5")

    return variants


# ============================================================================
# OPTIONAL PORT CHECK
# ============================================================================


def port_check(ip: str, port: int, timeout: float) -> bool:
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        return True
    except (OSError, TimeoutError, OverflowError):
        return False
    finally:
        if sock:
            sock.close()


# ============================================================================
# RESPONSE ERROR
# ============================================================================


def extract_error(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (
            (response.text or "").strip().replace("\n", " ")[:240]
        )
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            kind = error.get("type") or error.get("code") or "error"
            msg = error.get("message") or ""
            return f"{kind}: {msg}".strip(": ")[:240]
        if isinstance(error, str):
            return error[:240]
        for key in ("message", "detail"):
            if key in data:
                return str(data[key])[:240]
    return json.dumps(data, ensure_ascii=False)[:240]


# ============================================================================
# SINGLE PROXY TEST — MULTI-ENDPOINT
# ============================================================================


def _try_single_endpoint(
    proxy_url: str,
    test_url: str,
    model: str,
    api_key: str,
    read_timeout: int,
    connect_timeout: int,
    ping_prompt: str,
    max_tokens: int,
) -> tuple[bool, str, str, float, str]:
    """
    Try a single proxy_url + test_url combination.
    Returns (ok, stage, detail, latency, ai_text).
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/json, "
            "text/event-stream;q=0.9, "
            "application/x-ndjson;q=0.8, "
            "*/*;q=0.2"
        ),
        "Content-Type": "application/json",
        "Connection": "close",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = build_ai_payload(
        test_url, model, ping_prompt, max_tokens,
    )

    started = time.monotonic()
    proxies = {"http": proxy_url, "https": proxy_url}

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                test_url,
                json=payload,
                headers=headers,
                proxies=proxies,
                timeout=(connect_timeout, read_timeout),
                allow_redirects=False,
            )
    except requests.exceptions.SSLError as exc:
        return (
            False, "test_failed",
            "SSL: " + short(str(exc), 200),
            (time.monotonic() - started) * 1000, "",
        )
    except requests.exceptions.ProxyError as exc:
        return (
            False, "test_failed",
            "Proxy: " + short(str(exc), 200),
            (time.monotonic() - started) * 1000, "",
        )
    except requests.exceptions.ConnectionError as exc:
        return (
            False, "test_failed",
            "Conn: " + short(str(exc), 200),
            (time.monotonic() - started) * 1000, "",
        )
    except requests.exceptions.Timeout:
        return (
            False, "test_failed", "Timeout",
            (time.monotonic() - started) * 1000, "",
        )
    except requests.exceptions.RequestException as exc:
        return (
            False, "test_failed",
            f"{type(exc).__name__}: " + short(str(exc), 200),
            (time.monotonic() - started) * 1000, "",
        )

    latency = (time.monotonic() - started) * 1000

    if log.isEnabledFor(logging.DEBUG):
        body_preview = (
            (response.text or "")[:700].replace("\n", "\\n")
        )
        log.debug(
            "PROXY %s URL %s -> HTTP %d | CT=%s | BODY=%s",
            proxy_url, test_url,
            response.status_code,
            response.headers.get("Content-Type", "N/A"),
            body_preview,
        )

    # --- 2xx ---
    if 200 <= response.status_code < 300:
        ok, stage, detail, ai_text = validate_ai_response(
            response, ping_prompt, test_url,
        )
        return (ok, stage, detail, latency, ai_text)

    # --- HTTP error ---
    detail = extract_error(response)
    detail_lower = detail.lower()

    scheme_hint = (
        "plain http request was sent to an https server"
        in detail_lower
        or "client sent an http request to an https server"
        in detail_lower
        or "wrong version number" in detail_lower
        or "http request was sent to https port"
        in detail_lower
    )

    if scheme_hint:
        return (
            False, "test_failed",
            f"HTTP {response.status_code}: "
            f"{short(detail, 180)} "
            "[scheme mismatch]",
            latency, "",
        )

    if response.status_code == 407:
        return (
            False, "auth_required",
            "HTTP 407: " + short(detail, 180),
            latency, "",
        )

    if response.status_code == 429:
        return (
            False, "rate_limited",
            "HTTP 429: " + short(detail, 180),
            latency, "",
        )

    if response.status_code == 403:
        return (
            False, "region_blocked",
            "HTTP 403: " + short(detail, 180),
            latency, "",
        )

    if any(
        token in detail_lower
        for token in (
            "region", "geo",
            "country blocked", "location blocked",
        )
    ):
        return (
            False, "region_blocked",
            f"HTTP {response.status_code}: " + short(detail, 180),
            latency, "",
        )

    return (
        False, "api_error",
        f"HTTP {response.status_code}: " + short(detail, 180),
        latency, "",
    )


def test_proxy(
    entry: ProxyEntry,
    test_urls: list[str],
    model: str,
    api_key: str,
    read_timeout: int,
    connect_timeout: int,
    ping_prompt: str,
    max_tokens: int,
) -> tuple[bool, str, str, float, str, str, str]:
    """
    FIX v6.3: Multi-endpoint.
    Try all (proxy_scheme x test_url) combinations.
    Returns (ok, stage, detail, latency, ai_text, proxy_url, endpoint_url).
    """
    last_detail = "Unknown error"
    last_stage = "test_failed"
    last_latency = 0.0
    last_proxy = ""
    last_endpoint = ""

    for proxy_url in proxy_variants(entry):
        for test_url in test_urls:
            ok, stage, detail, latency, ai_text = _try_single_endpoint(
                proxy_url, test_url, model, api_key,
                read_timeout, connect_timeout, ping_prompt, max_tokens,
            )

            last_latency = latency
            last_proxy = proxy_url
            last_endpoint = test_url

            if ok:
                return (
                    True, "success",
                    f"{detail} via {proxy_url} [{endpoint_label(test_url)}]",
                    latency, ai_text, proxy_url, test_url,
                )

            # If scheme mismatch, try next proxy variant
            if "[scheme mismatch]" in detail:
                last_detail = detail
                last_stage = "test_failed"
                break  # break inner loop, try next proxy scheme

            # If auth/rate/region, no point trying other endpoints
            if stage in ("auth_required", "rate_limited", "region_blocked"):
                return (
                    False, stage, detail,
                    latency, "", proxy_url, test_url,
                )

            # For other errors, remember and try next endpoint
            last_detail = detail
            last_stage = stage

    return (
        False, last_stage, last_detail,
        last_latency, "", last_proxy, last_endpoint,
    )


# ============================================================================
# SINGLE ENTRY CHECK
# ============================================================================


def check_one(
    entry: ProxyEntry,
    test_urls: list[str],
    stop: Event,
    model: str,
    api_key: str,
    read_timeout: int,
    connect_timeout: int,
    skip_port_check: bool,
    ping_prompt: str,
    max_tokens: int,
    double_check: bool,
) -> CheckResult:
    if stop.is_set() or _shutdown.is_set():
        return CheckResult(entry, False, "skipped")

    if not skip_port_check and not port_check(
        entry.ip, entry.port, connect_timeout,
    ):
        return CheckResult(entry, False, "port_closed")

    if not proxy_variants(entry):
        return CheckResult(entry, False, "unsupported", entry.protocol)

    ok, stage, detail, latency, ai_text, proxy_url, endpoint_url = (
        test_proxy(
            entry, test_urls, model, api_key,
            read_timeout, connect_timeout, ping_prompt, max_tokens,
        )
    )

    if not ok:
        return CheckResult(
            entry, False, stage, detail, latency, "",
            proxy_url, endpoint_url,
        )

    # --- Double-check ---
    if double_check:
        if stop.is_set() or _shutdown.is_set():
            return CheckResult(entry, False, "skipped")
        time.sleep(0.25)
        (
            ok2, stage2, detail2, latency2,
            ai_text2, proxy_url2, endpoint_url2,
        ) = test_proxy(
            entry, test_urls, model, api_key,
            read_timeout, connect_timeout, ping_prompt, max_tokens,
        )
        if not ok2:
            return CheckResult(
                entry, False, stage2,
                f"Flaky 1/2: {detail2}",
                latency2, "",
                proxy_url2 or proxy_url,
                endpoint_url2 or endpoint_url,
            )
        if not ai_text or not ai_text2:
            return CheckResult(
                entry, False, "invalid_ai_response",
                "Verification tanpa output AI",
                latency2, "",
                proxy_url2 or proxy_url,
                endpoint_url2 or endpoint_url,
            )
        latency = (latency + latency2) / 2
        ai_text = ai_text2
        detail = detail2
        proxy_url = proxy_url2 or proxy_url
        endpoint_url = endpoint_url2 or endpoint_url

    return CheckResult(
        entry, True, "success", detail, latency,
        ai_text, proxy_url, endpoint_url,
    )


# ============================================================================
# PROBE MODE
# ============================================================================


def probe_proxy(
    proxy_url: str,
    test_urls: list[str],
    model: str,
    api_key: str,
    read_timeout: int,
    connect_timeout: int,
    ping_prompt: str,
    max_tokens: int,
) -> int:
    console.print(
        Rule("[bold bright_white]PROBE MODE[/]", style="cyan")
    )
    console.print()

    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim", width=18)
    info.add_column(style="white")
    info.add_row("Proxy", proxy_url)
    info.add_row("Endpoints", ", ".join(test_urls))
    info.add_row("Model", model)
    info.add_row("Prompt", f'"{ping_prompt}"')
    info.add_row("Max tokens", str(max_tokens))
    console.print(
        Panel(info, title="[bold]Probe config[/]",
              border_style="cyan", padding=(1, 2))
    )

    any_pass = False

    for test_url in test_urls:
        console.print()
        console.print(
            Rule(
                f"[bold]Testing: {endpoint_label(test_url)}[/] "
                f"[dim]({test_url})[/]",
                style="blue",
            )
        )

        payload = build_ai_payload(
            test_url, model, ping_prompt, max_tokens,
        )
        console.print("[dim]Request payload:[/]")
        console.print(
            Syntax(
                json.dumps(payload, indent=2, ensure_ascii=False),
                "json", theme="monokai",
            )
        )
        console.print()

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/json, "
                "text/event-stream;q=0.9, "
                "application/x-ndjson;q=0.8, "
                "*/*;q=0.2"
            ),
            "Content-Type": "application/json",
            "Connection": "close",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        console.print("[dim]Sending request...[/]")
        started = time.monotonic()

        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(
                    test_url,
                    json=payload,
                    headers=headers,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=(connect_timeout, read_timeout),
                    allow_redirects=False,
                )
        except Exception as exc:
            console.print(f"[bold red]Request failed:[/] {exc}")
            continue

        latency = (time.monotonic() - started) * 1000

        resp_info = Table.grid(padding=(0, 2))
        resp_info.add_column(style="dim", width=18)
        resp_info.add_column(style="white")
        resp_info.add_row("Status", str(response.status_code))
        resp_info.add_row("Latency", f"{latency:.0f} ms")
        resp_info.add_row(
            "Content-Type",
            response.headers.get("Content-Type", "N/A"),
        )
        resp_info.add_row(
            "Body size", f"{len(response.text or '')} bytes",
        )
        console.print()
        console.print(
            Panel(resp_info, title="[bold]Response[/]",
                  border_style="blue", padding=(1, 2))
        )

        raw = response.text or ""
        console.print(f"[dim]Raw body ({len(raw)} chars):[/]")
        if raw.strip():
            try:
                data = response.json()
                pretty = json.dumps(
                    data, indent=2, ensure_ascii=False,
                )
                console.print(
                    Syntax(pretty[:6000], "json", theme="monokai")
                )
                if len(pretty) > 6000:
                    console.print(
                        f"[dim]... truncated "
                        f"({len(pretty) - 6000} more chars)[/]"
                    )
            except ValueError:
                console.print(raw[:6000])
        else:
            console.print("[dim italic](empty)[/]")
        console.print()

        ok, stage, detail, ai_text = validate_ai_response(
            response, ping_prompt, test_url,
        )

        if ok:
            any_pass = True
            console.print(
                Panel(
                    f"[bold green]PASS[/] — {detail}\n"
                    f'AI text: [italic green]'
                    f'"{short(ai_text, 400)}"[/]',
                    title=(
                        f"[bold green]"
                        f"Validation Result "
                        f"[{endpoint_label(test_url)}]"
                        f"[/]"
                    ),
                    border_style="green", padding=(1, 2),
                )
            )
        else:
            label, style = STAGE_LABEL.get(stage, (stage, "white"))
            console.print(
                Panel(
                    f"[bold red]FAIL[/] [{label}] — {detail}",
                    title=(
                        f"[bold red]"
                        f"Validation Result "
                        f"[{endpoint_label(test_url)}]"
                        f"[/]"
                    ),
                    border_style="red", padding=(1, 2),
                )
            )

    console.print()
    if any_pass:
        console.print(
            "[bold green]Proxy VALID — "
            "mengembalikan output AI dari minimal satu endpoint.[/]"
        )
        return 0

    console.print(
        "[bold red]Proxy INVALID — "
        "tidak ada endpoint yang mengembalikan output AI.[/]"
    )
    return 1


# ============================================================================
# RICH UI
# ============================================================================


def terminal_header(args: argparse.Namespace) -> Panel:
    title = Text()
    title.append("PROXY FINDER", style="bold bright_white")
    title.append(f"  v{__version__}", style="cyan")
    title.append("  Multi-Endpoint AI Validator", style="dim")
    meta = Text()
    meta.append("Endpoints ", style="dim")
    meta.append(str(len(args.test_urls)), style="bright_cyan")
    meta.append("  •  Model ", style="dim")
    meta.append(args.model, style="bright_cyan")
    meta.append("  •  Workers ", style="dim")
    meta.append(str(args.workers), style="bright_cyan")
    meta.append("  •  Mode ", style="dim")
    meta.append("ALL" if args.all else "FIRST", style="bold yellow")
    return Panel(Group(title, meta), border_style="cyan", padding=(1, 2))


def render_dashboard(
    progress: Progress,
    task_id: int,
    checked: int,
    total: int,
    success: int,
    failed: int,
    started: float,
    last_result: Optional[CheckResult],
) -> Group:
    elapsed = max(0.001, time.monotonic() - started)
    speed = checked / elapsed

    stats = Table.grid(padding=(0, 2))
    for _ in range(4):
        stats.add_column()
    stats.add_row(
        Text(f"{checked}/{total}", style="bold white"),
        Text(f"✓ {success}", style="bold green"),
        Text(f"✗ {failed}", style="bold red"),
        Text(f"{speed:.1f}/s", style="cyan"),
    )

    status = Text("Last: ", style="dim")
    if last_result:
        label, style = STAGE_LABEL.get(
            last_result.stage, (last_result.stage, "white"),
        )
        status.append(label, style=style)
        status.append(f"  {last_result.entry.authority}", style="white")
        if last_result.endpoint_used:
            status.append(
                f"  [{endpoint_label(last_result.endpoint_used)}]",
                style="dim",
            )
        if last_result.latency_ms:
            status.append(
                f"  {last_result.latency_ms:.0f} ms", style="cyan",
            )
    else:
        status.append("initializing…")

    progress.update(task_id, description="Scanning proxies")

    return Group(
        Panel(stats, border_style="blue", padding=(0, 1)),
        progress,
        Panel(status, border_style="grey35", padding=(0, 1)),
    )


def render_result_table(
    results: list[CheckResult],
    max_rows: int = 80,
) -> Table:
    active = sorted(
        (r for r in results if r.ok),
        key=lambda x: x.latency_ms,
    )
    failed = sorted(
        (r for r in results if not r.ok),
        key=lambda x: (x.stage, x.latency_ms),
    )
    rows = (active + failed)[:max_rows]

    table = Table(
        title=(
            f"Results • {len(active)} active / {len(results)} checked"
        ),
        title_style="bold white",
        header_style="bold bright_white",
        border_style="grey35",
        box=None,
        pad_edge=False,
    )
    table.add_column("#", justify="right", width=4)
    table.add_column("Proto", style="cyan", width=7)
    table.add_column("Geo", width=7)
    table.add_column("Address", style="white")
    table.add_column("Endpoint", style="dim", width=8)
    table.add_column("Latency", justify="right", width=10)
    table.add_column("Status", width=13)
    table.add_column(
        "Detail / AI Response", style="dim", overflow="ellipsis",
    )

    for i, result in enumerate(rows, 1):
        label, style = STAGE_LABEL.get(
            result.stage, (result.stage, "white"),
        )
        table.add_row(
            str(i),
            result.entry.protocol,
            result.entry.country or "-",
            result.entry.authority,
            endpoint_label(result.endpoint_used) if result.endpoint_used else "-",
            f"{result.latency_ms:.0f} ms" if result.latency_ms else "-",
            Text(label, style=style),
            short(result.detail, 120),
        )

    if len(results) > len(rows):
        table.caption = (
            f"Showing {len(rows)} rows of {len(results)}. "
            f"Use --full-table or JSON for all."
        )

    return table


def render_success(
    result: CheckResult,
    show_auth: bool = False,
) -> Panel:
    details = Table.grid(padding=(0, 1))
    details.add_column(width=15, style="dim")
    details.add_column(style="white")
    details.add_row("Protocol", result.entry.protocol)
    details.add_row("Address", result.entry.authority)
    details.add_row("Country", result.entry.country or "-")
    details.add_row("Latency", f"{result.latency_ms:.0f} ms")
    details.add_row("Status", result.detail)
    details.add_row(
        "Endpoint",
        f"{endpoint_label(result.endpoint_used)} "
        f"({result.endpoint_used})"
        if result.endpoint_used else "-",
    )
    details.add_row(
        "AI Response",
        f'[italic green]"{short(result.ai_text, 260)}"[/]',
    )
    details.add_row(
        "Validated via",
        result.proxy_url or result.entry.curl_proxy or "-",
    )
    details.add_row(
        "Proxy string", result.entry.display_proxy(show_auth),
    )
    return Panel(
        details,
        title="[bold green]✓ Working AI proxy found[/]",
        border_style="green",
        padding=(1, 2),
    )


# ============================================================================
# DIAGNOSTICS
# ============================================================================


def failure_hint(results: list[CheckResult]) -> Optional[str]:
    counter = Counter(r.stage for r in results if not r.ok)
    hints: list[str] = []
    if counter.get("region_blocked"):
        hints.append(f"{counter['region_blocked']}× HTTP 403/GEO")
    if counter.get("rate_limited"):
        hints.append(f"{counter['rate_limited']}× HTTP 429")
    if counter.get("auth_required"):
        hints.append(f"{counter['auth_required']}× HTTP 407 Proxy Auth")
    if counter.get("api_error"):
        hints.append(f"{counter['api_error']}× API error")
    if counter.get("incomplete"):
        hints.append(
            f"{counter['incomplete']}× "
            "Response incomplete (tingkatkan --max-tokens)"
        )
    if counter.get("invalid_ai_response"):
        hints.append(
            f"{counter['invalid_ai_response']}× "
            "2xx tanpa output AI valid"
        )
    if counter.get("test_failed"):
        hints.append(
            f"{counter['test_failed']}× connection/request failed"
        )
    if counter.get("port_closed"):
        hints.append(f"{counter['port_closed']}× port closed")
    return "\n".join(hints) if hints else None


# ============================================================================
# SCANNER ENGINE
# ============================================================================


def _run_scan(
    entries: list[ProxyEntry],
    first_only: bool,
    test_urls: list[str],
    workers: int,
    model: str,
    api_key: str,
    read_timeout: int,
    connect_timeout: int,
    skip_port_check: bool,
    ping_prompt: str,
    max_tokens: int,
    double_check: bool,
) -> tuple[Optional[CheckResult], list[CheckResult]]:
    stop = Event()
    results: list[CheckResult] = []
    found: Optional[CheckResult] = None
    checked = 0
    success = 0
    failed = 0
    started = time.monotonic()
    last_result: Optional[CheckResult] = None
    batch_size = max(workers * DEFAULT_BATCH_MULTIPLIER, workers)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        expand=True,
    )

    with Live(console=console, refresh_per_second=6) as live:
        task_id = progress.add_task("Scanning proxies", total=len(entries))
        live.update(
            render_dashboard(
                progress, task_id, 0, len(entries), 0, 0, started, None,
            )
        )

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="proxy",
        ) as pool:
            iterator = iter(entries)
            pending: set[Future[CheckResult]] = set()

            def refill() -> None:
                while len(pending) < batch_size:
                    entry = next(iterator, None)
                    if entry is None:
                        return
                    future = pool.submit(
                        check_one,
                        entry, test_urls, stop, model, api_key,
                        read_timeout, connect_timeout, skip_port_check,
                        ping_prompt, max_tokens, double_check,
                    )
                    pending.add(future)

            refill()

            while pending:
                if _shutdown.is_set():
                    stop.set()
                    break

                done, _ = wait(
                    pending, timeout=0.5, return_when=FIRST_COMPLETED,
                )

                if not done:
                    live.update(
                        render_dashboard(
                            progress, task_id, checked, len(entries),
                            success, failed, started, last_result,
                        )
                    )
                    continue

                for future in done:
                    pending.discard(future)
                    checked += 1
                    progress.advance(task_id)

                    try:
                        result = future.result()
                    except Exception as exc:
                        log.exception("worker failed")
                        dummy = ProxyEntry("", "", "", 0)
                        result = CheckResult(
                            dummy, False, "test_failed", str(exc),
                        )

                    results.append(result)
                    last_result = result

                    if result.ok:
                        success += 1
                        found = result
                        if first_only:
                            stop.set()
                    else:
                        failed += 1

                live.update(
                    render_dashboard(
                        progress, task_id, checked, len(entries),
                        success, failed, started, last_result,
                    )
                )

                if first_only and found:
                    break

                refill()

            for future in pending:
                future.cancel()

        live.update(
            render_dashboard(
                progress, task_id, checked, len(entries),
                success, failed, started, last_result,
            )
        )

    return (found, results)


def run_first_match(
    *args, **kwargs,
) -> tuple[Optional[CheckResult], list[CheckResult]]:
    return _run_scan(*args, first_only=True, **kwargs)


def run_check_all(
    *args, **kwargs,
) -> tuple[Optional[CheckResult], list[CheckResult]]:
    return _run_scan(*args, first_only=False, **kwargs)


# ============================================================================
# OUTPUT FILES
# ============================================================================


def atomic_write_text(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def save_proxy_list(
    path: str,
    results: list[CheckResult],
    show_auth: bool = True,
) -> int:
    active = sorted(
        (r for r in results if r.ok and r.proxy_url),
        key=lambda x: x.latency_ms,
    )
    lines: list[str] = []
    for result in active:
        proxy = result.proxy_url
        if not show_auth:
            proxy = result.entry.display_proxy(False)
        lines.append(proxy)
    atomic_write_text(
        path, "\n".join(lines) + ("\n" if lines else ""),
    )
    return len(lines)


def save_omniroute(path: str, results: list[CheckResult]) -> None:
    active = sorted(
        (r for r in results if r.ok),
        key=lambda r: (r.entry.protocol, r.entry.ip, r.entry.port),
    )
    lines = [
        "# Proxy Bulk Import",
        "# Format: NAME|HOST|PORT|USERNAME|PASSWORD|TYPE|REGION|STATUS|NOTES",
        "# Required: NAME, HOST, PORT",
        "",
    ]
    for result in active:
        e = result.entry
        proxy_type = (
            e.protocol
            if e.protocol in ("http", "https", "socks5")
            else "socks5"
        )
        lines.append("|".join([
            f"proxy-{e.ip}-{e.port}",
            e.ip,
            str(e.port),
            e.username or "",
            e.password or "",
            proxy_type,
            e.country,
            "active",
            (
                "AI verified twice"
                if "Flaky" not in result.detail
                else "AI verified"
            ),
        ]))
    atomic_write_text(path, "\n".join(lines) + "\n")


def save_json(
    path: str,
    results: list[CheckResult],
    total_loaded: int,
    sources: list[str],
    args: argparse.Namespace,
) -> None:
    counter = Counter(r.stage for r in results)
    active = sorted(
        (r for r in results if r.ok),
        key=lambda x: x.latency_ms,
    )
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "validation_policy": (
            "active_only_when_valid_assistant_output_exists"
        ),
        "config": {
            "test_urls": args.test_urls,
            "model": args.model,
            "workers": args.workers,
            "protocol": args.protocol,
            "double_check": args.double_check,
            "skip_port_check": args.skip_port_check,
            "all_sources": args.all_sources,
            "sources": sources,
        },
        "stats": {
            "total_loaded": total_loaded,
            "total_checked": len(results),
            "success": counter.get("success", 0),
            "incomplete": counter.get("incomplete", 0),
            "invalid_ai_response": counter.get("invalid_ai_response", 0),
            "region_blocked": counter.get("region_blocked", 0),
            "rate_limited": counter.get("rate_limited", 0),
            "auth_required": counter.get("auth_required", 0),
            "api_error": counter.get("api_error", 0),
            "test_failed": counter.get("test_failed", 0),
            "port_closed": counter.get("port_closed", 0),
            "unsupported": counter.get("unsupported", 0),
            "skipped": counter.get("skipped", 0),
        },
        "active_proxies": [
            {
                "protocol": r.entry.protocol,
                "ip": r.entry.ip,
                "port": r.entry.port,
                "country": r.entry.country,
                "has_auth": r.entry.has_auth,
                "latency_ms": round(r.latency_ms, 1),
                "proxy": r.proxy_url,
                "endpoint": r.endpoint_used,
                "status": r.detail,
                "ai_text": r.ai_text,
            }
            for r in active
        ],
    }
    atomic_write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False),
    )


# ============================================================================
# CLI
# ============================================================================


def parse_test_urls(value: str) -> list[str]:
    """Parse comma-separated test URLs."""
    urls = [u.strip() for u in value.split(",") if u.strip()]
    return urls or DEFAULT_TEST_URLS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"Proxy Finder v{__version__} — "
            "Multi-endpoint AI proxy checker"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "test_url",
        nargs="?",
        default=None,
        help=(
            "Target URL(s), dipisahkan koma. "
            "Default: kedua endpoint OpenCode"
        ),
    )
    parser.add_argument(
        "--source", choices=sorted(SOURCE_PRESETS), default=None,
    )
    parser.add_argument(
        "--all-sources",
        action="store_true",
        help="Gunakan semua source proxy yang tersedia",
    )
    parser.add_argument(
        "--json-url", default=None,
        help="URL source dipisahkan koma",
    )
    parser.add_argument(
        "--protocol", choices=sorted(PROTOCOL_SCHEME), default=None,
    )
    parser.add_argument(
        "--model",
        default=os.getenv("PROXY_TEST_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument("--ping-prompt", default=DEFAULT_PING_PROMPT)
    parser.add_argument(
        "--max-tokens", type=int, default=4096,
        help=(
            "Max output tokens. "
            "Nilai rendah menyebabkan 'incomplete' response"
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("PROXY_TEST_API_KEY", ""),
    )
    parser.add_argument(
        "--max-time", type=int, default=DEFAULT_MAX_TIME,
        help="Read timeout per AI request",
    )
    parser.add_argument(
        "--connect-timeout", type=int, default=CONNECT_TIMEOUT,
    )
    parser.add_argument(
        "--source-timeout", type=int, default=SOURCE_TIMEOUT,
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Scan semua proxy yang berhasil dimuat",
    )
    parser.add_argument("--output", default="proxies.txt")
    parser.add_argument("--omniroute-output", default="omniroute.txt")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("-s", "--shuffle", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json-output", default=None, metavar="FILE")
    parser.add_argument(
        "--skip-port-check",
        action="store_true",
        default=DEFAULT_SKIP_PORT_CHECK,
        help="Jangan lakukan TCP port-check terpisah",
    )
    parser.add_argument(
        "--port-check",
        dest="skip_port_check",
        action="store_false",
        help="Aktifkan TCP port-check",
    )
    parser.add_argument("--show-auth", action="store_true")
    parser.add_argument("--full-table", action="store_true")
    parser.add_argument(
        "--double-check",
        dest="double_check",
        action="store_true",
        default=True,
        help="Verifikasi AI dua kali sebelum active",
    )
    parser.add_argument(
        "--no-double-check",
        dest="double_check",
        action="store_false",
        help="Matikan verifikasi kedua",
    )
    parser.add_argument(
        "--probe",
        default=None,
        metavar="PROXY_URL",
        help="Debug satu proxy di semua endpoint",
    )

    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.workers < 1:
        raise ValueError("--workers harus >= 1")
    if args.max_time < 1:
        raise ValueError("--max-time harus >= 1")
    if args.connect_timeout < 1:
        raise ValueError("--connect-timeout harus >= 1")
    if args.source_timeout < 1:
        raise ValueError("--source-timeout harus >= 1")
    if args.max_tokens < 1:
        raise ValueError("--max-tokens harus >= 1")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit harus >= 1")
    if not args.test_urls:
        raise ValueError("Minimal satu test URL diperlukan")


# ============================================================================
# MAIN
# ============================================================================


def main() -> int:
    started = time.monotonic()
    args = build_parser().parse_args()

    # --- Resolve test URLs ---
    env_url = os.getenv("PROXY_TEST_URL", "")
    if args.test_url:
        args.test_urls = parse_test_urls(args.test_url)
    elif env_url:
        args.test_urls = parse_test_urls(env_url)
    else:
        args.test_urls = list(DEFAULT_TEST_URLS)

    try:
        validate_args(args)
    except ValueError as exc:
        console.print(f"[bold red]Argument error:[/] {exc}")
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- Probe mode ---
    if args.probe:
        return probe_proxy(
            args.probe, args.test_urls, args.model, args.api_key,
            args.max_time, args.connect_timeout, args.ping_prompt,
            args.max_tokens,
        )

    # --- Header ---
    console.clear()
    console.print(terminal_header(args))

    # --- Sources ---
    sources = resolve_sources(
        args.source, args.json_url, args.all_sources,
    )

    source_text = Text()
    source_text.append("Sources ", style="bold")
    source_text.append(str(len(sources)), style="cyan")
    source_text.append("  •  ", style="dim")
    source_text.append("Timeout ", style="bold")
    source_text.append(
        f"{args.connect_timeout}s / {args.max_time}s", style="cyan",
    )
    source_text.append("  •  ", style="dim")
    source_text.append("Endpoints ", style="bold")
    source_text.append(str(len(args.test_urls)), style="cyan")
    source_text.append("  •  ", style="dim")
    source_text.append("Double-check ", style="bold")
    source_text.append(
        "ON" if args.double_check else "OFF",
        style="green" if args.double_check else "yellow",
    )
    console.print(Panel(source_text, border_style="grey35", padding=(0, 1)))

    # --- Load ---
    try:
        with console.status("[cyan]Loading proxy sources…[/]", spinner="dots"):
            entries, per_source, load_errors = load_entries(
                sources, args.protocol or "", args.source_timeout,
            )
    except Exception as exc:
        console.print(
            Panel(str(exc), title="[bold red]LOAD ERROR[/]", border_style="red")
        )
        return 1

    if load_errors:
        table = Table(
            title="Source warnings", border_style="yellow", box=None,
        )
        table.add_column("Source", style="yellow")
        table.add_column("Error", style="dim")
        for error in load_errors[:15]:
            name, _, detail = error.partition(" -> ")
            table.add_row(name, short(detail, 120))
        console.print(table)

    if not entries:
        console.print(
            Panel(
                "Tidak ada proxy valid yang berhasil diparse dari source.",
                title="[bold red]NO PROXIES[/]",
                border_style="red",
            )
        )
        return 1

    # --- Shuffle / limit ---
    if args.shuffle:
        random.shuffle(entries)
    if args.limit:
        entries = entries[: args.limit]

    # --- SOCKS dependency ---
    if not socks_ok(entries):
        console.print(
            Panel(
                'Proxy SOCKS terdeteksi tetapi PySocks belum terpasang.\n\n'
                'Jalankan: pip install "requests[socks]"',
                title="[bold red]DEPENDENCY REQUIRED[/]",
                border_style="red",
            )
        )
        return 1

    # --- Configuration overview ---
    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column(style="bold white")
    proto_counter = Counter(e.protocol for e in entries)
    overview.add_row("Loaded", str(len(entries)))
    overview.add_row(
        "Protocols",
        ", ".join(f"{k}={v}" for k, v in proto_counter.most_common()),
    )
    overview.add_row("Sources ok", str(len(per_source)))
    overview.add_row("Endpoints", str(len(args.test_urls)))
    for url in args.test_urls:
        overview.add_row(
            "",
            f"  {endpoint_label(url)}: {short(url, 90)}",
        )
    overview.add_row("Model", args.model)
    overview.add_row("Ping prompt", f'"{args.ping_prompt}"')
    overview.add_row("Max tokens", str(args.max_tokens))
    overview.add_row("Proxy mode", "AUTO HTTP/HTTPS + SOCKS")
    overview.add_row(
        "Port pre-check", "OFF" if args.skip_port_check else "ON",
    )
    overview.add_row(
        "Validation",
        (
            "MULTI-ENDPOINT + SCHEMA + ASSISTANT OUTPUT "
            "+ ECHO REJECT + DOUBLE CHECK"
            if args.double_check
            else "MULTI-ENDPOINT + SCHEMA + ASSISTANT OUTPUT "
            "+ ECHO REJECT"
        ),
    )
    console.print(
        Panel(
            overview,
            title="[bold bright_white]Scan configuration[/]",
            border_style="blue",
        )
    )
    console.print()

    # --- Scan arguments ---
    scan_kwargs = dict(
        entries=entries,
        test_urls=args.test_urls,
        workers=args.workers,
        model=args.model,
        api_key=args.api_key,
        read_timeout=args.max_time,
        connect_timeout=args.connect_timeout,
        skip_port_check=args.skip_port_check,
        ping_prompt=args.ping_prompt,
        max_tokens=args.max_tokens,
        double_check=args.double_check,
    )

    # --- ALL ---
    if args.all:
        _, results = run_check_all(**scan_kwargs)
        console.print()
        console.print(
            render_result_table(
                results, len(results) if args.full_table else 80,
            )
        )
        active = sorted(
            (r for r in results if r.ok), key=lambda x: x.latency_ms,
        )
        save_proxy_list(args.output, results, show_auth=True)
        save_omniroute(args.omniroute_output, results)
        if args.json_output:
            save_json(
                args.json_output, results, len(entries), sources, args,
            )
        if active:
            console.print(Rule("Fastest working proxy", style="green"))
            console.print(render_success(active[0], args.show_auth))

        elapsed = time.monotonic() - started
        console.print(
            Panel(
                f"[bold green]{len(active)}[/] active / "
                f"[bold]{len(results)}[/] checked\n"
                f"Elapsed: [cyan]{fmt_duration(elapsed)}[/]\n"
                f"Output: [bright_white]{args.output}  •  "
                f"{args.omniroute_output}[/]",
                title=(
                    "[bold green]Scan complete[/]"
                    if active
                    else "[bold yellow]Scan complete[/]"
                ),
                border_style="green" if active else "yellow",
            )
        )
        if not active:
            hint = failure_hint(results)
            if hint:
                console.print(
                    Panel(
                        hint,
                        title="[bold yellow]Why no active proxy[/]",
                        border_style="yellow",
                    )
                )
        return 0 if active else 1

    # --- FIRST ---
    result, results = run_first_match(**scan_kwargs)
    console.print()

    if result:
        console.print(render_success(result, args.show_auth))
        if args.json_output:
            save_json(
                args.json_output, results, len(entries), sources, args,
            )
            console.print(
                f"[dim]JSON saved → {args.json_output}[/]"
            )

        elapsed_done = time.monotonic() - started
        console.print(
            f"[dim]Completed in {fmt_duration(elapsed_done)}[/]"
        )
        return 0

    console.print(
        Panel(
            "Tidak ada proxy yang menghasilkan output AI assistant yang valid.",
            title="[bold yellow]NO WORKING AI PROXY[/]",
            border_style="yellow",
        )
    )
    hint = failure_hint(results)
    if hint:
        console.print(
            Panel(
                hint,
                title="[bold yellow]Diagnostic[/]",
                border_style="yellow",
            )
        )
    if args.json_output:
        save_json(
            args.json_output, results, len(entries), sources, args,
        )
        console.print(
            f"[dim]JSON saved → {args.json_output}[/]"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())