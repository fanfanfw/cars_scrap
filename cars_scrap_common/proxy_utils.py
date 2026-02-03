from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse

from cars_scrap_common.env_utils import env_bool


@dataclass(frozen=True)
class ProxySpec:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    scheme: str = "http"

    def to_url(self) -> str:
        if self.username is None and self.password is None:
            return f"{self.scheme}://{self.host}:{self.port}"
        user = quote(self.username or "", safe="")
        pwd = quote(self.password or "", safe="")
        return f"{self.scheme}://{user}:{pwd}@{self.host}:{self.port}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_proxy_state_path() -> Path:
    return _repo_root() / "profiles" / "proxy_state.json"


def parse_proxy_items(raw: str | None, *, scheme: str = "http") -> list[str]:
    """
    Supports:
    - Comma-separated list of proxy URLs: http://user:pass@host:port
    - Comma-separated list of IP:PORT:USER:PASS (Webshare format)
    - Comma-separated list of HOST:PORT
    """
    value = (raw or "").strip()
    if not value:
        return []

    out: list[str] = []
    for item in value.split(","):
        token = (item or "").strip()
        if not token:
            continue

        if "://" in token:
            parsed = urlparse(token)
            if parsed.scheme and parsed.netloc:
                out.append(token)
                continue

        parts = token.split(":")
        if len(parts) == 2:
            host, port_s = parts
            port = int(port_s)
            out.append(ProxySpec(host=host, port=port, scheme=scheme).to_url())
            continue
        if len(parts) == 4:
            host, port_s, user, pwd = parts
            port = int(port_s)
            out.append(ProxySpec(host=host, port=port, username=user, password=pwd, scheme=scheme).to_url())
            continue

        raise ValueError(f"Invalid proxy entry: {token!r}. Expected URL or HOST:PORT or HOST:PORT:USER:PASS.")

    seen: set[str] = set()
    unique: list[str] = []
    for url in out:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def pick_next_proxy(proxies: Iterable[str], *, state_path: Path | None = None) -> str | None:
    items = [p for p in proxies if p and str(p).strip()]
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    state_path = state_path or default_proxy_state_path()

    idx = 0
    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(raw)
        idx = int(state.get("idx", 0))
    except Exception:
        idx = 0

    picked = items[idx % len(items)]
    _atomic_write_json(state_path, {"idx": (idx + 1) % len(items), "count": len(items)})
    return picked


def load_proxy_for_run() -> str | None:
    """
    Loads PROXY_SERVER/PROXY_SERVERS and returns a single proxy URL to be used for the entire run.
    Rotation is round-robin using a small state file under ./profiles.
    """
    enabled_raw = os.getenv("PROXY_ENABLED")
    if enabled_raw is not None and not env_bool("PROXY_ENABLED", True):
        return None

    global _CACHED_PROXY, _CACHED_PROXY_SET
    if _CACHED_PROXY_SET:
        return _CACHED_PROXY

    raw = os.getenv("PROXY_SERVER") or os.getenv("PROXY_SERVERS") or ""
    scheme = (os.getenv("PROXY_SCHEME") or "http").strip() or "http"

    proxies = parse_proxy_items(raw, scheme=scheme)
    if not proxies:
        _CACHED_PROXY = None
        _CACHED_PROXY_SET = True
        return None

    state_override = (os.getenv("PROXY_STATE_FILE") or "").strip()
    state_path = Path(state_override) if state_override else None
    _CACHED_PROXY = pick_next_proxy(proxies, state_path=state_path)
    _CACHED_PROXY_SET = True
    return _CACHED_PROXY


_CACHED_PROXY: str | None = None
_CACHED_PROXY_SET = False


def mask_proxy_url(proxy_url: str) -> str:
    try:
        parsed = urlparse(proxy_url)
    except Exception:
        return "<invalid-proxy>"
    if not parsed.scheme:
        return proxy_url
    host = parsed.hostname or ""
    port = parsed.port
    if parsed.username or parsed.password:
        user = parsed.username or ""
        netloc = f"{user}:***@{host}"
    else:
        netloc = host
    if port:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme}://{netloc}"


def probe_public_ip(*, check_url: str, proxy_url: str | None, timeout_seconds: float = 7.0) -> str | None:
    """
    Returns the current public IP as seen by `check_url`.
    If `proxy_url` is provided, the request is made via the proxy.
    """
    try:
        import requests
    except Exception:
        return None

    url = (check_url or "").strip()
    if not url:
        return None

    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(url, proxies=proxies, timeout=timeout_seconds, headers={"User-Agent": "cars-scrap"})
        resp.raise_for_status()
        text = (resp.text or "").strip()
        return text.splitlines()[0].strip() if text else None
    except Exception:
        return None

