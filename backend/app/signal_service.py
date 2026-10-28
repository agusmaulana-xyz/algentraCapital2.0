import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .gemini_parser import GeminiParser, SignalClassification, not_signal
from .models import AppSetting, Signal, SystemLog, utc_now


DEFAULT_SYMBOL_MAPPING = {"XAUUSD": "XAUUSD", "GOLD": "XAUUSD", "XAU": "XAUUSD", "EMAS": "XAUUSD"}
_TRADE_ACTION_RE = re.compile(r"\b(?:BUY|SELL|LONG|SHORT|BELI|JUAL)\b", re.IGNORECASE)
_SIGNAL_MARKER_RE = re.compile(
    r"\b(?:TP\s*[\d⁰¹²³⁴⁵⁶⁷⁸⁹]*|TAKE[\s_-]*PROFIT|TARGET\s*[\d⁰¹²³⁴⁵⁶⁷⁸⁹]*|SL|STOP[\s_-]*LOSS|ENTRY|ZONE|NOW|MARKET|LIMIT|STOP)\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)?")
_TRADE_RESULT_RE = re.compile(
    r"\b(?:TP\s*[\d⁰¹²³⁴⁵⁶⁷⁸⁹]*|SL|STOP\s*LOSS)\s*(?:HIT|KEN[A]?|REACHED|TERCAPAI)\b|"
    r"\b(?:CLOSED|CLOSE|PROFIT|LOSS)\s+(?:TRADE|POSITION|POSISI)\b|"
    r"\b(?:CLOSE|TUTUP)\s+(?:NOW|ALL|POSITION|POSISI|SEKARANG|SEMUA)\b|"
    r"\b(?:MOVE|ADJUST|GESER|PINDAH|UBAH)\b.{0,30}\b(?:SL|STOP\s*LOSS|BE|BREAKEVEN)\b",
    re.IGNORECASE,
)


def _signal_candidate(raw_text: str, context: str | None) -> tuple[bool, str]:
    """Pass trade-shaped messages to the parser regardless of the instrument."""
    text = raw_text.strip()
    if not text:
        return False, "Pesan kosong"
    if _TRADE_RESULT_RE.search(text):
        return False, "Laporan hasil trade, bukan sinyal baru"
    if not _TRADE_ACTION_RE.search(text) and not (context and _TRADE_ACTION_RE.search(context)):
        return False, "Tidak ada arah BUY/SELL"
    if not _PRICE_RE.search(text) and not re.search(r"\b(?:NOW|MARKET)\b", text, re.IGNORECASE):
        return False, "Tidak ada harga atau instruksi market"
    if not _SIGNAL_MARKER_RE.search(text) and not _TRADE_ACTION_RE.search(text):
        return False, "Tidak ada format harga/level sinyal"
    return True, "Kandidat sinyal trading"


@dataclass
class ValidationResult:
    status: str
    classification: SignalClassification
    symbol: str | None
    normalized_text: str | None
    reason: str


@dataclass
class ProcessResult:
    status: str
    signal_id: int | None
    classification: SignalClassification | None
    normalized_text: str | None
    reason: str
    duplicate: bool = False


def _setting(db: Session, key: str, default):
    item = db.get(AppSetting, key)
    if item is None:
        return default
    try:
        return json.loads(item.value)
    except (TypeError, json.JSONDecodeError):
        return default


def normalize_symbol(symbol: str | None, mapping: dict[str, str] | None = None, default_symbol: str = "XAUUSD") -> str:
    if not symbol:
        return default_symbol.upper()
    original = re.sub(r"\s+", "", symbol)
    candidate = original.upper()
    aliases = {**DEFAULT_SYMBOL_MAPPING, **{str(key).upper(): str(value).upper() for key, value in (mapping or {}).items()}}
    for alias in sorted(aliases, key=len, reverse=True):
        if candidate == alias:
            return aliases[alias]
        if candidate.startswith(alias):
            suffix = candidate[len(alias) :]
            if suffix and (suffix.isalnum() or suffix.startswith(".")):
                return f"{aliases[alias]}{original[len(alias) :]}"
    return candidate


def _is_xauusd_symbol(symbol: str) -> bool:
    return symbol.upper().startswith("XAUUSD")


def _is_gold_symbol_alias(symbol: str | None) -> bool:
    if not symbol:
        return True
    candidate = re.sub(r"\s+", "", symbol).upper()
    return candidate.startswith(("XAUUSD", "GOLD", "XAU", "EMAS"))


def _prefer_farthest_target_when_needed(classification: SignalClassification) -> SignalClassification:
    """Keep the final TP when nearer targets cannot satisfy this signal's SL risk."""
    targets = classification.tp or []
    if (
        classification.type != "NEW_SIGNAL"
        or classification.action not in {"BUY", "SELL"}
        or classification.sl is None
        or not math.isfinite(classification.sl)
        or classification.sl <= 0
        or len(targets) < 2
        or any(not math.isfinite(target) or target <= 0 for target in targets)
    ):
        return classification

    farthest = max(targets) if classification.action == "BUY" else min(targets)
    reference = classification.entry
    if classification.entry_low is not None and classification.entry_high is not None:
        reference = classification.entry_high if classification.action == "BUY" else classification.entry_low

    # Market signals have no entry reference yet. Keep the final target so the EA
    # can validate its SL/TP distance against the live quote before placing an order.
    if reference is None:
        if classification.action == "BUY" and classification.sl >= min(targets):
            return classification
        if classification.action == "SELL" and classification.sl <= max(targets):
            return classification
        return classification.model_copy(update={
            "tp": [farthest],
            "reason": f"Target terjauh {farthest:g} dipilih; EA memvalidasi rasio risiko dari quote live",
        })
    if not math.isfinite(reference) or reference <= 0:
        return classification
    if classification.action == "BUY" and (
        classification.sl >= reference or any(target <= reference for target in targets)
    ):
        return classification
    if classification.action == "SELL" and (
        classification.sl <= reference or any(target >= reference for target in targets)
    ):
        return classification

    nearest = min(targets, key=lambda target: abs(target - reference))
    risk_distance = abs(reference - classification.sl)
    nearest_reward = abs(nearest - reference)
    farthest_reward = abs(farthest - reference)
    if risk_distance <= nearest_reward or risk_distance > farthest_reward:
        return classification

    return classification.model_copy(update={
        "tp": [farthest],
        "reason": f"Target terjauh {farthest:g} dipilih karena TP terdekat lebih pendek dari risiko SL",
    })


def validate_classification(
    classification: SignalClassification,
    *,
    default_symbol: str = "XAUUSD",
    confidence_threshold: float = 0.75,
    symbol_mapping: dict[str, str] | None = None,
    allow_updates: bool = False,
    market_price: float | None = None,
    max_market_deviation_pct: float = 5.0,
) -> ValidationResult:
    if not classification.is_signal or classification.type == "NOT_SIGNAL":
        return ValidationResult("IGNORED", classification, None, None, classification.reason or "Bukan sinyal")
    if classification.type == "UPDATE" and not allow_updates:
        return ValidationResult("IGNORED", classification, None, None, "Signal UPDATE dinonaktifkan di settings")
    if classification.type != "NEW_SIGNAL":
        return ValidationResult("REJECTED", classification, None, None, "Jenis signal tidak dikenal")
    if classification.confidence < confidence_threshold:
        return ValidationResult("REJECTED", classification, None, None, "Confidence di bawah threshold")
    if classification.action not in {"BUY", "SELL"}:
        return ValidationResult("REJECTED", classification, None, None, "Action BUY/SELL tidak valid")
    if classification.order_type not in {"MARKET", "LIMIT", "STOP", "AUTO"}:
        return ValidationResult("REJECTED", classification, None, None, "Order type tidak valid")
    has_entry_range = classification.entry_low is not None or classification.entry_high is not None
    if has_entry_range and (classification.entry_low is None or classification.entry_high is None):
        return ValidationResult("REJECTED", classification, None, None, "Zona entry harus memiliki batas bawah dan atas")
    if classification.order_type == "AUTO" and not has_entry_range:
        return ValidationResult("REJECTED", classification, None, None, "AUTO memerlukan zona entry")
    if classification.order_type in {"LIMIT", "STOP"} and classification.entry is None:
        return ValidationResult("REJECTED", classification, None, None, "Pending order wajib memiliki harga entry")
    if has_entry_range and classification.entry is not None:
        return ValidationResult("REJECTED", classification, None, None, "Gunakan entry tunggal atau zona entry, jangan keduanya")

    action = classification.action
    entry = classification.entry
    entry_low = classification.entry_low
    entry_high = classification.entry_high
    if entry_low is not None and entry_high is not None:
        entry_low, entry_high = sorted((entry_low, entry_high))
    stop_loss = classification.sl
    take_profits = classification.tp or []
    prices = take_profits + [price for price in (stop_loss, entry, entry_low, entry_high) if price is not None]
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        return ValidationResult("REJECTED", classification, None, None, "Harga harus lebih besar dari nol")
    if entry_low is not None and entry_high is not None:
        if action == "BUY" and ((stop_loss is not None and stop_loss >= entry_low) or any(tp <= entry_high for tp in take_profits)):
            return ValidationResult("REJECTED", classification, None, None, "Zona BUY mensyaratkan SL di bawah batas bawah dan setiap TP di atas batas atas")
        if action == "SELL" and ((stop_loss is not None and stop_loss <= entry_high) or any(tp >= entry_low for tp in take_profits)):
            return ValidationResult("REJECTED", classification, None, None, "Zona SELL mensyaratkan SL di atas batas atas dan setiap TP di bawah batas bawah")

    if entry is not None:
        if action == "BUY" and ((stop_loss is not None and stop_loss >= entry) or any(tp <= entry for tp in take_profits)):
            return ValidationResult("REJECTED", classification, None, None, "BUY mensyaratkan SL < entry < setiap TP")
        if action == "SELL" and ((stop_loss is not None and stop_loss <= entry) or any(tp >= entry for tp in take_profits)):
            return ValidationResult("REJECTED", classification, None, None, "SELL mensyaratkan setiap TP < entry < SL")
        if market_price and market_price > 0:
            deviation_pct = abs(entry - market_price) / market_price * 100.0
            if deviation_pct > max_market_deviation_pct:
                return ValidationResult("REJECTED", classification, None, None, "Entry terlalu jauh dari harga pasar")
    elif stop_loss is not None and take_profits:
        if action == "BUY" and stop_loss >= min(take_profits):
            return ValidationResult("REJECTED", classification, None, None, "BUY mensyaratkan SL di bawah TP")
        if action == "SELL" and max(take_profits) >= stop_loss:
            return ValidationResult("REJECTED", classification, None, None, "SELL mensyaratkan TP di bawah SL")

    if not _is_gold_symbol_alias(classification.symbol):
        return ValidationResult("REJECTED", classification, classification.symbol, None, "Hanya signal XAUUSD yang diterima")
    symbol = normalize_symbol(classification.symbol, symbol_mapping, default_symbol)
    if not _is_xauusd_symbol(symbol):
        return ValidationResult("REJECTED", classification, symbol, None, "Hanya signal XAUUSD yang diterima")

    if stop_loss is not None and take_profits:
        reference = entry_high if action == "BUY" and entry_high is not None else (
            entry_low if action == "SELL" and entry_low is not None else entry
        )
        if reference is not None:
            risk_distance = abs(reference - stop_loss)
            reward_distance = min(abs(target - reference) for target in take_profits)
            if risk_distance > reward_distance:
                return ValidationResult("REJECTED", classification, symbol, None, "Jarak SL lebih besar daripada jarak TP")

    levels = []
    if entry_low is not None and entry_high is not None:
        levels.append(f"ENTRY ZONE : {entry_low:g}–{entry_high:g}")
    elif entry is not None:
        levels.append(f"ENTRY : {entry:g}")
    elif classification.order_type == "MARKET":
        levels.append("MARKET")
    if stop_loss is not None:
        levels.append(f"SL : {stop_loss:g}")
    levels.extend(f"TP{index} : {price:g}" for index, price in enumerate(take_profits, start=1))
    normalized = f"{action} : " + " | ".join(levels)
    return ValidationResult("PENDING", classification, symbol, normalized, "Signal valid")


class SignalService:
    def __init__(self, parser: GeminiParser | None = None) -> None:
        self.parser = parser or GeminiParser()

    async def process_message(
        self,
        db: Session,
        *,
        group_id: str,
        group_name: str,
        message_id: str,
        sender_id: str | None = None,
        sender_name: str | None = None,
        raw_text: str,
        created_at: datetime | None = None,
        context: str | None = None,
    ) -> ProcessResult:
        stamp = created_at or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        existing = db.execute(
            select(Signal).where(Signal.group_id == str(group_id), Signal.message_id == str(message_id))
        ).scalar_one_or_none()
        if existing:
            return ProcessResult(existing.status, existing.id, None, existing.normalized_text, "Message sudah diproses", True)

        content = re.sub(r"\s+", " ", raw_text).strip().casefold()
        time_bucket = stamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
        content_hash = hashlib.sha256(f"{group_id}|{time_bucket}|{content}".encode("utf-8")).hexdigest()
        duplicate = db.execute(select(Signal).where(Signal.content_hash == content_hash)).scalar_one_or_none()
        if duplicate:
            return ProcessResult(duplicate.status, duplicate.id, None, duplicate.normalized_text, "Signal identik dalam jendela waktu yang sama", True)

        should_parse, filter_reason = _signal_candidate(raw_text, context)
        if not should_parse:
            classification = not_signal(f"Gemini dilewati: {filter_reason}")
        else:
            try:
                classification = await self.parser.parse(raw_text, context)
            except Exception as exc:
                row = Signal(
                    group_id=str(group_id),
                    group_name=group_name,
                    message_id=str(message_id),
                    sender_id=str(sender_id) if sender_id is not None else None,
                    sender_name=sender_name,
                    raw_text=raw_text[:12000],
                    content_hash=content_hash,
                    status="FAILED",
                    created_at=stamp,
                )
                db.add(row)
                db.add(SystemLog(level="ERROR", source="Gemini", message=f"Parser gagal: {exc}"[:2000], created_at=utc_now()))
                db.commit()
                db.refresh(row)
                return ProcessResult("FAILED", row.id, None, None, "Parser tidak tersedia")

        classification = _prefer_farthest_target_when_needed(classification)

        mapping = _setting(db, "symbol_mapping", {})
        if not isinstance(mapping, dict):
            mapping = {}
        confidence_threshold = float(_setting(db, "confidence_threshold", 0.75))
        allow_updates = bool(_setting(db, "allow_updates", False))
        max_deviation = float(_setting(db, "max_market_deviation_pct", 5.0))
        validation = validate_classification(
            classification,
            default_symbol="XAUUSD",
            confidence_threshold=confidence_threshold,
            symbol_mapping=mapping,
            allow_updates=allow_updates,
            max_market_deviation_pct=max_deviation,
        )
        saved_classification = classification.model_copy(update={"symbol": validation.symbol or classification.symbol})
        row = Signal(
            group_id=str(group_id),
            group_name=group_name,
            message_id=str(message_id),
            sender_id=str(sender_id) if sender_id is not None else None,
            sender_name=sender_name,
            raw_text=raw_text[:12000],
            parsed_json=saved_classification.model_dump_json(),
            normalized_text=validation.normalized_text,
            confidence=classification.confidence,
            content_hash=content_hash,
            status=validation.status,
            created_at=stamp,
        )
        db.add(row)
        db.add(SystemLog(
            level="INFO" if validation.status in {"PENDING", "IGNORED"} else "WARN",
            source="Signal",
            message=f"{group_name}: {validation.status} — {validation.reason}"[:2000],
            created_at=utc_now(),
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicate = db.execute(select(Signal).where(Signal.content_hash == content_hash)).scalar_one_or_none()
            if duplicate:
                return ProcessResult(duplicate.status, duplicate.id, None, duplicate.normalized_text, "Signal duplikat", True)
            raise
        db.refresh(row)
        return ProcessResult(validation.status, row.id, saved_classification, validation.normalized_text, validation.reason)
