"""Signal schema and validation for the JSONL transport."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping


class SignalValidationError(ValueError):
    """Raised when a JSONL signal is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class Signal:
    v: int
    id: str
    ts: float
    aksi: str
    pos_id: int
    simbol: str | None = None
    arah: str | None = None
    lot: float | None = None
    harga: float | None = None
    sl: float | None = None
    tp: float | None = None
    komentar: str = ""
    lot_sisa: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "v": self.v,
            "id": self.id,
            "ts": self.ts,
            "aksi": self.aksi,
            "pos_id": self.pos_id,
        }
        for key in ("simbol", "arah", "lot", "harga", "sl", "tp", "komentar", "lot_sisa"):
            value = getattr(self, key)
            if value is not None or key == "komentar":
                result[key] = value
        return result


_REQUIRED: dict[str, tuple[str, ...]] = {
    "buka": ("pos_id", "simbol", "arah", "lot", "sl", "tp"),
    "ubah": ("pos_id", "sl", "tp"),
    "kurang": ("pos_id", "lot", "lot_sisa"),
    "tutup": ("pos_id",),
}


def _number(data: Mapping[str, Any], key: str, *, required: bool = False) -> float | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalValidationError(f"field '{key}' harus berupa angka")
    result = float(value)
    if not math.isfinite(result):
        raise SignalValidationError(f"field '{key}' harus berupa angka finite")
    return result


def _positive_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SignalValidationError(f"field '{key}' harus berupa integer positif")
    return value


def parse_signal(raw: str | bytes | Mapping[str, Any] | Signal) -> Signal:
    """Parse and validate a signal, raising a readable error for bad input."""
    if isinstance(raw, Signal):
        raw = raw.to_dict()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SignalValidationError("baris bukan UTF-8 yang valid") from exc
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SignalValidationError(f"JSON tidak valid: {exc.msg}") from exc
    else:
        data = raw
    if not isinstance(data, Mapping):
        raise SignalValidationError("sinyal harus berupa object JSON")

    version = data.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise SignalValidationError("field 'v' harus bernilai 1")
    signal_id = data.get("id")
    if not isinstance(signal_id, str) or not signal_id.strip():
        raise SignalValidationError("field 'id' harus berupa string yang tidak kosong")
    ts = _number(data, "ts", required=True)
    action = data.get("aksi")
    if not isinstance(action, str) or action not in _REQUIRED:
        raise SignalValidationError("field 'aksi' harus salah satu dari buka, ubah, kurang, tutup")
    pos_id = _positive_int(data, "pos_id")

    for field in _REQUIRED[action]:
        if field not in data or data[field] is None:
            raise SignalValidationError(f"field '{field}' wajib untuk aksi '{action}'")

    symbol = data.get("simbol")
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise SignalValidationError("field 'simbol' harus berupa string yang tidak kosong")
    side = data.get("arah")
    if side is not None and side not in ("buy", "sell"):
        raise SignalValidationError("field 'arah' harus 'buy' atau 'sell'")
    comment = data.get("komentar", "")
    if not isinstance(comment, str):
        raise SignalValidationError("field 'komentar' harus berupa string")

    lot = _number(data, "lot", required=action in ("buka", "kurang"))
    price = _number(data, "harga")
    sl = _number(data, "sl", required=action in ("buka", "ubah"))
    tp = _number(data, "tp", required=action in ("buka", "ubah"))
    remaining = _number(data, "lot_sisa", required=action == "kurang")
    if action == "buka":
        if lot is None or lot <= 0:
            raise SignalValidationError("field 'lot' untuk buka harus lebih besar dari 0")
    if action == "kurang":
        if lot is None or lot <= 0 or remaining is None or remaining < 0:
            raise SignalValidationError("lot kurang harus positif dan lot_sisa tidak boleh negatif")
    return Signal(
        v=1,
        id=signal_id,
        ts=float(ts),
        aksi=action,
        pos_id=pos_id,
        simbol=symbol,
        arah=side,
        lot=lot,
        harga=price,
        sl=sl,
        tp=tp,
        komentar=comment,
        lot_sisa=remaining,
    )


def encode_signal(signal: Signal | Mapping[str, Any]) -> str:
    """Return one compact, validated JSON object without a newline."""
    parsed = parse_signal(signal)
    return json.dumps(parsed.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
