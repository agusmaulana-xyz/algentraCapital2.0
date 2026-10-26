"""Cached, server-side lookup for MetaTrader 5 broker servers."""

from threading import Lock
from time import monotonic

import httpx


DIRECTORY_URL = "https://mt5.mtapi.io/Search"
CACHE_TTL_SECONDS = 1800
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_cache_lock = Lock()


class BrokerDirectoryError(Exception):
    pass


def search_mt5_servers(query: str) -> list[dict[str, str]]:
    query = query.strip()
    if not 3 <= len(query) <= 96:
        return []
    key = query.casefold()
    now = monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    try:
        response = httpx.get(
            DIRECTORY_URL,
            params={"company": query},
            headers={"Accept": "application/json"},
            timeout=8.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BrokerDirectoryError from exc

    if not isinstance(payload, list):
        raise BrokerDirectoryError

    seen: set[str] = set()
    needle = query.casefold()
    servers: list[dict[str, str]] = []
    for company in payload:
        if not isinstance(company, dict):
            continue
        company_name = company.get("companyName")
        results = company.get("results")
        if not isinstance(company_name, str) or not isinstance(results, list):
            continue
        company_name = company_name.strip()[:128]
        if not company_name:
            continue
        for server in results:
            if not isinstance(server, dict):
                continue
            name = server.get("name")
            if not isinstance(name, str):
                continue
            name = name.strip()[:128]
            unique_key = f"{company_name.casefold()}|{name.casefold()}"
            if not name or unique_key in seen:
                continue
            seen.add(unique_key)
            servers.append({"name": name, "company": company_name})

    servers.sort(key=lambda item: (item["name"].casefold() != needle, item["name"].casefold()))
    servers = servers[:200]
    with _cache_lock:
        _cache[key] = (monotonic(), servers)
        if len(_cache) > 200:
            expired = [item for item, value in _cache.items() if monotonic() - value[0] >= CACHE_TTL_SECONDS]
            for item in expired:
                _cache.pop(item, None)
            while len(_cache) > 200:
                _cache.pop(next(iter(_cache)))
    return servers
