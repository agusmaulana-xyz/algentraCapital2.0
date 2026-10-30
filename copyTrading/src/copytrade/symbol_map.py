"""Broker symbol discovery and master-to-follower translation."""

from __future__ import annotations

from typing import Any, Mapping


_CENT_CURRENCIES = {"USC", "USDC", "EUC", "GBC", "GBPC", "AUC", "AUDC", "CADC", "JPYC"}
_CURRENCY_SUFFIXES = {
    "AUD", "CAD", "CHF", "CNH", "CNY", "EUR", "GBP", "HKD", "JPY", "MXN",
    "NOK", "NZD", "PLN", "RUB", "SEK", "SGD", "TRY", "USD", "ZAR",
}


def map_symbol(master_symbol: str, config: Mapping[str, Any]) -> str:
    mappings = config.get("map_simbol", {})
    if master_symbol in mappings:
        return str(mappings[master_symbol])
    upper = str(master_symbol).strip().upper()
    if upper.startswith("XAUUSD") and "XAUUSD" in mappings:
        suffix = upper[len("XAUUSD"):]
        follower_symbol = str(mappings["XAUUSD"])
        # Preserve a numeric broker suffix such as XAUUSD247 on regular
        # accounts, where that exact symbol may have its own trading session.
        # resolve_symbol() searches the broker's available XAUUSD variants.
        if suffix.isdigit() and follower_symbol.strip().upper() == "XAUUSD":
            return str(master_symbol)
        return follower_symbol
    return str(master_symbol)


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def account_uses_cent_symbols(account: Any) -> bool:
    currency = str(_value(account, "currency", "") or "").strip().upper()
    if currency in _CENT_CURRENCIES or "CENT" in currency:
        return True
    identity = " ".join(
        str(_value(account, key, "") or "")
        for key in ("name", "server", "company")
    ).upper()
    return "CENT" in identity


def is_cent_gold_symbol(symbol: str, account: Any = None) -> bool:
    """Return whether a resolved gold symbol belongs to a cent account."""
    upper = str(symbol or "").strip().upper()
    if upper == "XAUUSDC":
        return True
    return upper.startswith("XAUUSD") and account_uses_cent_symbols(account)


def _symbol_root(requested: str) -> str:
    upper = requested.strip().upper()
    # Broker gold names can have arbitrary suffixes (XAUUSDm, XAUUSD.c,
    # XAUUSD-pro, and so on). Keep actual currency crosses as exact requests.
    if upper.startswith("XAUUSD"):
        suffix = upper[6:]
        if suffix and len(suffix) == 3 and suffix in _CURRENCY_SUFFIXES:
            return upper
        return "XAUUSD"
    return upper


def _looks_like_requested_instrument(name: str, requested: str, root: str, info: Any) -> bool:
    upper = name.strip().upper()
    requested_upper = requested.strip().upper()
    if upper == requested_upper or upper.startswith(root):
        suffix = upper[len(root):] if upper.startswith(root) else ""
        # Do not mistake a cross such as XAUUSDCAD for a suffix variant.
        if len(suffix) == 3 and suffix in _CURRENCY_SUFFIXES:
            return False
        return True
    if root == "XAUUSD":
        return (
            str(_value(info, "currency_base", "") or "").upper() == "XAU"
            and str(_value(info, "currency_profit", "") or "").upper() == "USD"
        )
    return False


def _candidate_symbols(mt5: Any, requested: str, root: str) -> list[Any]:
    candidates: dict[str, Any] = {}
    exact = mt5.symbol_info(requested)
    if exact is not None:
        exact_name = str(_value(exact, "name", requested) or requested)
        candidates[exact_name.casefold()] = exact

    symbols_get = getattr(mt5, "symbols_get", None)
    if callable(symbols_get):
        groups = [f"{root}*"]
        if root == "XAUUSD":
            groups.append("*GOLD*")
        for group in groups:
            try:
                symbols = symbols_get(group=group)
            except TypeError:
                try:
                    symbols = symbols_get()
                except Exception:
                    symbols = None
            except Exception:
                symbols = None
            for info in symbols or ():
                name = str(_value(info, "name", "") or "").strip()
                if name and _looks_like_requested_instrument(name, requested, root, info):
                    candidates[name.casefold()] = info
    return list(candidates.values())


def resolve_symbol(mt5: Any, requested: str, account: Any = None) -> str:
    """Resolve an exact symbol or the best broker variant for this account.

    Any broker symbol beginning with XAUUSD can be selected, including custom
    suffixes used by cent and standard accounts. An explicit available symbol
    is preferred; otherwise live quotes and the broker's available variants
    determine the result.
    """
    requested = str(requested).strip()
    if not requested:
        raise ValueError("Nama simbol follower kosong")
    cent_account = account_uses_cent_symbols(account)
    root = _symbol_root(requested)
    candidates = _candidate_symbols(mt5, requested, root)
    explicit_variant = requested.upper() != root.upper()
    ranked: list[tuple[tuple[Any, ...], str]] = []
    disabled = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)

    for candidate in candidates:
        name = str(_value(candidate, "name", requested) or requested).strip()
        if not _looks_like_requested_instrument(name, requested, root, candidate):
            continue
        info = mt5.symbol_info(name) or candidate
        trade_mode = _value(info, "trade_mode")
        if trade_mode is not None and int(trade_mode) == int(disabled):
            continue
        tick = None
        tick_reader = getattr(mt5, "symbol_info_tick", None)
        if callable(tick_reader):
            try:
                tick = tick_reader(name)
            except Exception:
                tick = None
        bid = float(_value(tick, "bid", 0.0) or 0.0)
        ask = float(_value(tick, "ask", 0.0) or 0.0)
        has_quote = bid > 0 and ask > 0
        tick_time = int(_value(tick, "time_msc", 0) or _value(tick, "time", 0) or 0)
        suffix = name.upper()[len(root):] if name.upper().startswith(root) else ""
        direct_match_rank = 0 if name.upper().startswith(root) else 1
        requested_rank = 0 if explicit_variant and name.casefold() == requested.casefold() else 1
        # For cent accounts, prefer a broker-suffixed instrument when no
        # explicit variant was configured, while still using any available
        # XAUUSD variant if the broker has no suffix at all.
        variant_rank = (0 if suffix else 1) if cent_account else (0 if not suffix else 1)
        score = (requested_rank, -int(has_quote), -tick_time, variant_rank, direct_match_rank, len(suffix), name.casefold())
        ranked.append((score, name))

    if not ranked:
        if root == "XAUUSD":
            raise ValueError(
                f"Simbol '{requested}' tidak ditemukan atau nonaktif. Worker mencari otomatis semua varian simbol XAUUSD pada broker."
            )
        raise ValueError(f"Simbol '{requested}' tidak ditemukan atau nonaktif di terminal follower")
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def select_symbol(mt5: Any, symbol: str, account: Any = None) -> str:
    resolved = resolve_symbol(mt5, symbol, account)
    info = mt5.symbol_info(resolved)
    if info is None:
        # Some MT5 terminals return no SymbolInfo until the broker symbol has
        # first been added to Market Watch. Ask MT5 to select it, then query it
        # again before the follower reads prices or sends an order.
        if not mt5.symbol_select(resolved, True):
            raise ValueError(f"Gagal memasukkan simbol '{resolved}' ke Market Watch: {mt5.last_error()}")
        info = mt5.symbol_info(resolved)
        if info is None:
            raise ValueError(f"Simbol '{resolved}' tidak tersedia setelah ditambahkan ke Market Watch")
    if not getattr(info, "visible", False) and not mt5.symbol_select(resolved, True):
        raise ValueError(f"Gagal menampilkan simbol '{resolved}' di Market Watch: {mt5.last_error()}")
    return resolved
