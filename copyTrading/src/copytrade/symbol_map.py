"""Broker symbol discovery and master-to-follower translation."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping


CANONICAL_GOLD = "XAUUSD"
_CENT_CURRENCIES = {"USC", "USDC", "EUC", "GBC", "GBPC", "AUC", "AUDC", "CADC", "JPYC"}
_CURRENCY_SUFFIXES = {
    "AUD", "CAD", "CHF", "CNH", "CNY", "EUR", "GBP", "HKD", "JPY", "MXN",
    "NOK", "NZD", "PLN", "RUB", "SEK", "SGD", "TRY", "USD", "ZAR",
}
_DERIVATIVE_MARKERS = ("FUT", "FUTURE", "FUTURES", "FWD", "FORWARD", "OPT", "OPTION", "OPTIONS", "BINARY")
_UNSAFE_GOLD_NAME = re.compile(r"(?i)^XAUUSD_o")


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_symbol(name: str) -> str:
    """Uppercase a broker symbol and remove every non-alphanumeric character."""
    return "".join(char for char in str(name).upper() if char.isalnum())


def _is_gold_request(name: str) -> bool:
    original = str(name).strip()
    upper = original.upper()
    normalized = normalize_symbol(original)
    if upper.startswith("XAUUSD_O"):
        return False
    if normalized.startswith(CANONICAL_GOLD):
        suffix = normalized[len(CANONICAL_GOLD):]
        if len(suffix) == 3 and suffix in _CURRENCY_SUFFIXES:
            return False
        return not any(marker in suffix for marker in _DERIVATIVE_MARKERS) and "CFD" not in suffix
    if normalized.startswith("GOLD"):
        suffix = normalized[len("GOLD"):]
        return not any(marker in suffix for marker in _DERIVATIVE_MARKERS) and "CFD" not in suffix
    return False


def map_symbol(master_symbol: str, config: Mapping[str, Any]) -> str:
    """Translate a source symbol to the canonical pair or explicit non-gold map.

    Gold symbols are always reduced to XAUUSD here. The terminal-local suffix is
    resolved by :class:`SymbolResolver`, so a master and follower can use
    different broker names without a user-maintained mapping.
    """
    source = str(master_symbol).strip()
    if _is_gold_request(source):
        return CANONICAL_GOLD
    normalized = normalize_symbol(source)
    if normalized.startswith("XAU") or normalized.startswith("GOLD"):
        raise ValueError(f"Simbol sumber '{source}' bukan pasangan emas spot XAUUSD")
    mappings = config.get("map_simbol", {})
    if source in mappings:
        return str(mappings[source])
    for key, value in mappings.items():
        if str(key).strip().casefold() == source.casefold():
            return str(value)
    return source


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
    normalized = normalize_symbol(symbol)
    if normalized == "XAUUSDC":
        return True
    return normalized.startswith(CANONICAL_GOLD) and account_uses_cent_symbols(account)


def _currency_pair_is_gold_usd(info: Any) -> bool | None:
    base = str(_value(info, "currency_base", "") or "").strip().upper()
    quote = str(_value(info, "currency_profit", "") or "").strip().upper()
    if not base and not quote:
        return None
    return base == "XAU" and quote == "USD"


def _is_derivative(name: str, info: Any, mt5: Any = None) -> bool:
    upper = str(name).strip().upper()
    if _UNSAFE_GOLD_NAME.search(upper):
        return True
    normalized = normalize_symbol(upper)
    suffix = normalized[len(CANONICAL_GOLD):] if normalized.startswith(CANONICAL_GOLD) else ""
    descriptor = " ".join(
        str(_value(info, key, "") or "")
        for key in ("description", "path", "exchange")
    ).upper()
    text = suffix + " " + descriptor
    if any(marker in text for marker in _DERIVATIVE_MARKERS):
        return True
    # A generic description may call spot gold a CFD. Reject explicit CFD
    # product suffixes, while still accepting a broker's ordinary XAUUSD spot
    # contract whose description happens to mention CFD trading.
    if "CFD" in suffix:
        return True
    calculation_mode = _value(info, "trade_calc_mode")
    futures_modes = {
        mode
        for mode in (
            getattr(mt5, "SYMBOL_CALC_MODE_FUTURES", None),
            getattr(mt5, "SYMBOL_CALC_MODE_EXCH_FUTURES", None),
        )
        if mode is not None
    }
    if calculation_mode in futures_modes:
        return True
    expiration = _value(info, "expiration_time", 0)
    try:
        if expiration not in (None, "", 0, "0") and int(expiration) > 0:
            return True
    except (TypeError, ValueError):
        # Unreadable contract metadata is not enough to reject a conventional
        # spot symbol; the broker name and currencies are checked separately.
        pass
    return False


def _candidate_kind(name: str, info: Any, mt5: Any = None) -> str | None:
    original = str(name).strip()
    normalized = normalize_symbol(original)
    if _is_derivative(original, info, mt5):
        return None
    if normalized.startswith(CANONICAL_GOLD):
        suffix = normalized[len(CANONICAL_GOLD):]
        # Exclude currency crosses such as XAUUSDCAD and XAUUSDEUR.
        if len(suffix) == 3 and suffix in _CURRENCY_SUFFIXES:
            return None
        pair_check = _currency_pair_is_gold_usd(info)
        if pair_check is False:
            return None
        return "xauusd"
    # GOLD aliases are only a fallback and need positive broker metadata to
    # prove that the instrument is XAU priced in USD.
    if normalized.startswith("GOLD") and _currency_pair_is_gold_usd(info) is True:
        return "gold"
    return None


def _nearby_names(symbols: tuple[Any, ...] | list[Any]) -> list[str]:
    names = []
    for info in symbols:
        name = str(_value(info, "name", "") or "").strip()
        normalized = normalize_symbol(name)
        if name and ("XAU" in normalized or "GOLD" in normalized):
            names.append(name)
    return sorted(set(names), key=lambda value: (len(value), value.casefold()))[:8]


def _best_candidate(mt5: Any, symbols: tuple[Any, ...] | list[Any]) -> Any | None:
    full_trade_mode = int(getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4))
    candidates: list[tuple[tuple[Any, ...], Any]] = []
    preferred: list[Any] = []
    fallback: list[Any] = []
    for info in symbols:
        name = str(_value(info, "name", "") or "").strip()
        kind = _candidate_kind(name, info, mt5) if name else None
        if kind is None:
            continue
        (preferred if kind == "xauusd" else fallback).append(info)

    choices = preferred or fallback
    for info in choices:
        name = str(_value(info, "name", "") or "").strip()
        normalized = normalize_symbol(name)
        trade_mode = _value(info, "trade_mode")
        try:
            fully_tradable = trade_mode is not None and int(trade_mode) == full_trade_mode
        except (TypeError, ValueError):
            fully_tradable = False
        visible = bool(_value(info, "visible", False))
        selected = bool(_value(info, "select", visible))
        upper_name = name.upper()
        if upper_name.startswith(CANONICAL_GOLD):
            suffix_length = len(name) - len(CANONICAL_GOLD)
        elif normalized.startswith(CANONICAL_GOLD):
            suffix_length = max(0, len(normalized) - len(CANONICAL_GOLD))
        elif normalized.startswith("GOLD"):
            suffix_length = max(0, len(name) - len("GOLD"))
        else:
            suffix_length = 0
        score = (not (visible and fully_tradable), suffix_length, not selected, name.casefold())
        candidates.append((score, info))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


class SymbolResolver:
    """Resolve and cache this terminal session's local XAUUSD symbol.

    Each refresh reads the broker symbol catalog exactly once. Candidate
    matching and ranking use the returned records in memory; no per-candidate
    symbol or quote queries are made.
    """

    def __init__(
        self,
        mt5: Any,
        *,
        account: Any = None,
        broker: str = "unknown",
        logger_: logging.Logger | None = None,
        retry_interval: float = 15.0,
    ) -> None:
        self.mt5 = mt5
        self.account = account
        self.broker = broker or "unknown"
        self.logger = logger_ or logging.getLogger(__name__)
        self.retry_interval = max(0.0, retry_interval)
        self._resolved: str | None = None
        self._symbols: tuple[Any, ...] = ()
        self._last_attempt = 0.0
        self._last_warning = ""

    @property
    def resolved(self) -> str | None:
        return self._resolved

    def reset(self, *, account: Any = None, broker: str | None = None) -> None:
        self.account = account
        if broker is not None:
            self.broker = broker or "unknown"
        self._resolved = None
        self._symbols = ()
        self._last_attempt = 0.0
        self._last_warning = ""

    def refresh(self, *, force: bool = False) -> str | None:
        if self._resolved:
            return self._resolved
        now = time.monotonic()
        if not force and now - self._last_attempt < self.retry_interval:
            return None
        self._last_attempt = now
        symbols_get = getattr(self.mt5, "symbols_get", None)
        if not callable(symbols_get):
            self._log_failure("terminal MT5 tidak menyediakan symbols_get", [])
            return None
        try:
            symbols = tuple(symbols_get() or ())
        except Exception:
            self.logger.exception("Gagal membaca daftar simbol MT5 broker=%s", self.broker)
            self._log_failure("daftar simbol MT5 gagal dibaca", [])
            return None
        self._symbols = symbols
        chosen = _best_candidate(self.mt5, symbols)
        if chosen is None:
            self._log_failure("simbol belum tersedia", _nearby_names(symbols))
            return None
        resolved = str(_value(chosen, "name", "") or "").strip()
        visible = bool(_value(chosen, "visible", False))
        if not visible:
            try:
                selected = bool(self.mt5.symbol_select(resolved, True))
            except Exception:
                self.logger.exception("Gagal mengaktifkan simbol MT5 %s broker=%s", resolved, self.broker)
                selected = False
            if not selected:
                self._log_failure(
                    f"gagal mengaktifkan simbol {resolved} di Market Watch",
                    _nearby_names(symbols),
                )
                return None
        self._resolved = resolved
        self._last_warning = ""
        self.logger.info("Simbol emas MT5 terpilih broker=%s simbol=%s", self.broker, resolved)
        return resolved

    def resolve(self, requested: str = CANONICAL_GOLD, *, force: bool = False) -> str | None:
        if not _is_gold_request(requested):
            return None
        return self.refresh(force=force)

    def _log_failure(self, reason: str, candidates: list[str]) -> None:
        candidate_text = ", ".join(candidates) if candidates else "tidak ada kandidat dekat"
        message = f"{reason}; broker={self.broker}; kandidat terdekat=[{candidate_text}]"
        self.logger.warning("Simbol emas belum tersedia: %s", message)
        self._last_warning = message


def resolve_symbol(mt5: Any, requested: str, account: Any = None) -> str:
    """Resolve from one full broker catalog read and return the selected name."""
    requested = str(requested).strip()
    if not requested:
        raise ValueError("Nama simbol follower kosong")
    if _is_gold_request(requested):
        resolver = SymbolResolver(mt5, account=account)
        resolved = resolver.resolve(requested, force=True)
        if resolved is None:
            nearby = _nearby_names(resolver._symbols)
            candidates = ", ".join(nearby) if nearby else "tidak ada kandidat dekat"
            raise ValueError(
                f"Simbol emas '{requested}' tidak tersedia atau tidak terverifikasi; kandidat terdekat: {candidates}"
            )
        return resolved

    # Other configured instruments remain exact-only. The catalog is still
    # fetched once rather than probing variants one at a time.
    symbols_get = getattr(mt5, "symbols_get", None)
    try:
        symbols = tuple(symbols_get() or ()) if callable(symbols_get) else ()
    except Exception as exc:
        raise ValueError(f"Gagal membaca daftar simbol MT5: {exc}") from exc
    match = next(
        (info for info in symbols if str(_value(info, "name", "") or "").strip().casefold() == requested.casefold()),
        None,
    )
    if match is None:
        # Backward-compatible exact lookup for lightweight terminal wrappers
        # that do not expose a complete catalog. Gold resolution never takes
        # this path.
        try:
            match = mt5.symbol_info(requested)
        except Exception:
            match = None
    if match is None:
        raise ValueError(f"Simbol '{requested}' tidak ditemukan di terminal follower")
    resolved = str(_value(match, "name", requested) or requested).strip()
    if not bool(_value(match, "visible", False)) and not mt5.symbol_select(resolved, True):
        raise ValueError(f"Gagal memasukkan simbol '{resolved}' ke Market Watch: {mt5.last_error()}")
    return resolved


def select_symbol(mt5: Any, symbol: str, account: Any = None) -> str:
    """Compatibility wrapper that selects a single resolved gold symbol."""
    resolved = resolve_symbol(mt5, symbol, account)
    return resolved
