from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse

from cars_scrap_common.env_utils import env_bool


DEFAULT_PROXY_POOLS = {
    "carlist": "MY",
    "mudahmy": "MY",
    "goonet": "JP",
}


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


def default_proxy_pool_state_path(pool: str) -> Path:
    safe_pool = "".join(ch.lower() if ch.isalnum() else "_" for ch in pool).strip("_")
    return _repo_root() / "profiles" / f"proxy_state_{safe_pool or 'default'}.json"


def _normalize_service_name(service_name: str | None) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in (service_name or "")).strip("_")


def _normalize_pool_name(pool: str | None) -> str | None:
    value = (pool or "").strip().upper()
    if not value:
        return None
    if value == "IND":
        return "ID"
    return value


def resolve_proxy_pool(service_name: str | None) -> str | None:
    service = _normalize_service_name(service_name)
    if not service:
        return None

    override = os.getenv(f"PROXY_POOL_{service.upper()}")
    if override is not None:
        return _normalize_pool_name(override)

    return DEFAULT_PROXY_POOLS.get(service, "ID")


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


def load_proxy_for_run(*, service_name: str | None = None) -> str | None:
    """
    Loads a proxy URL to be used for the entire run.

    If `service_name` is provided, PROXY_POOL_<SERVICE> selects a pool and then
    PROXY_SERVER_<POOL>/PROXY_SERVERS_<POOL> is preferred. Defaults route
    carlist/mudahmy to MY and all other services to ID. Falls back to the global
    PROXY_SERVER/PROXY_SERVERS when no pool-specific proxy is configured.
    Rotation is round-robin using a small state file under ./profiles.
    """
    enabled_raw = os.getenv("PROXY_ENABLED")
    if enabled_raw is not None and not env_bool("PROXY_ENABLED", True):
        return None

    pool = resolve_proxy_pool(service_name)
    cache_key = pool or "__global__"
    if cache_key in _CACHED_PROXIES:
        return _CACHED_PROXIES[cache_key]

    raw = ""
    raw_from_pool = False
    if pool:
        raw = os.getenv(f"PROXY_SERVER_{pool}") or os.getenv(f"PROXY_SERVERS_{pool}") or ""
        raw_from_pool = bool(raw.strip())
    if not raw:
        raw = os.getenv("PROXY_SERVER") or os.getenv("PROXY_SERVERS") or ""

    scheme = ""
    if pool:
        scheme = os.getenv(f"PROXY_SCHEME_{pool}") or ""
    scheme = (scheme or os.getenv("PROXY_SCHEME") or "http").strip() or "http"

    proxies = parse_proxy_items(raw, scheme=scheme)
    if not proxies:
        _CACHED_PROXIES[cache_key] = None
        return None

    state_override = ""
    if pool:
        state_override = os.getenv(f"PROXY_STATE_FILE_{pool}") or ""
    state_override = (state_override or os.getenv("PROXY_STATE_FILE") or "").strip()
    if state_override:
        state_path = Path(state_override)
    elif pool and raw_from_pool:
        state_path = default_proxy_pool_state_path(pool)
    else:
        state_path = None

    proxy = pick_next_proxy(proxies, state_path=state_path)
    _CACHED_PROXIES[cache_key] = proxy
    return proxy


def reset_cached_proxy() -> None:
    _CACHED_PROXIES.clear()


_CACHED_PROXIES: dict[str, str | None] = {}


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
