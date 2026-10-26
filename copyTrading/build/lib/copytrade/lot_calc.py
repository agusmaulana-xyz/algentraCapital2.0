"""Safe lot sizing and volume-step normalization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class LotResult:
    volume: float | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.volume is not None


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def calculate_lot(
    lot_master: float,
    symbol_info: Any,
    config: Mapping[str, Any],
    *,
    balance_follower: float | None = None,
    balance_master: float | None = None,
) -> LotResult:
    """Calculate a volume; return a skip reason instead of forcing minimum lot."""
    try:
        master = float(lot_master)
        step = float(_get(symbol_info, "volume_step"))
        minimum = float(_get(symbol_info, "volume_min"))
        maximum = float(_get(symbol_info, "volume_max"))
    except (TypeError, ValueError):
        return LotResult(None, "informasi volume simbol tidak valid")
    if not all(math.isfinite(x) for x in (master, step, minimum, maximum)) or master <= 0 or step <= 0 or minimum <= 0 or maximum < minimum:
        return LotResult(None, "parameter lot atau volume simbol tidak valid")

    mode = config.get("mode_lot", "rasio")
    try:
        if mode == "tetap":
            raw = float(config["lot_tetap"])
        elif mode == "rasio":
            raw = master * float(config.get("rasio_lot", 1.0))
        elif mode == "saldo":
            master_balance = balance_master if balance_master is not None else config.get("saldo_master")
            if balance_follower is None or master_balance is None or float(master_balance) <= 0:
                return LotResult(None, "saldo follower/master tidak tersedia untuk mode saldo")
            raw = master * (float(balance_follower) / float(master_balance))
        else:
            return LotResult(None, f"mode_lot tidak dikenal: {mode}")
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return LotResult(None, "konfigurasi lot tidak valid")
    if not math.isfinite(raw) or raw <= 0:
        return LotResult(None, "hasil lot tidak valid")
    try:
        cap = float(config.get("max_lot_per_order", maximum))
    except (TypeError, ValueError):
        return LotResult(None, "max_lot_per_order tidak valid")
    if not math.isfinite(cap) or cap <= 0:
        return LotResult(None, "max_lot_per_order harus lebih besar dari 0")

    try:
        step_decimal = Decimal(str(step))
        bounded = min(Decimal(str(raw)), Decimal(str(maximum)), Decimal(str(cap)))
        units = (bounded / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
        volume_decimal = units * step_decimal
    except (InvalidOperation, ZeroDivisionError):
        return LotResult(None, "parameter volume_step tidak valid")
    volume = float(volume_decimal)
    if volume + step * 1e-9 < minimum:
        return LotResult(None, f"hasil lot {volume:g} di bawah volume_min {minimum:g}")
    return LotResult(volume, "ok")
