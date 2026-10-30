import csv
import hashlib
import io
import secrets
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import EALicenseKey, ScalperTrade, Signal, SystemLog, Trade, utc_now
from ..routers.tg import telegram_manager
from ..risk_controls import risk_state
from ..schemas import LogResponse, SignalResponse
from ..stats_service import get_dashboard_stats, get_group_stats, get_scalper_stats
from ..time_utils import WIB, as_utc, wib_datetime_to_utc_naive, wib_iso


router = APIRouter(prefix="/api", tags=["dashboard"])


class EALicenseKeyCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    expires_on: date
    expires_time: time | None = None


@router.get("/admin/ea-keys")
def list_ea_license_keys(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    now = utc_now()
    keys = db.execute(
        select(EALicenseKey).order_by(EALicenseKey.created_at.desc(), EALicenseKey.id.desc())
    ).scalars()
    return [
        {
            "id": key.id,
            "label": key.label,
            "fingerprint": key.key_hash[-8:].upper(),
            "created_at": wib_iso(key.created_at),
            "expires_at": wib_iso(key.expires_at),
            "expired": key.expires_at is not None and as_utc(key.expires_at) <= now,
            "last_validated_at": wib_iso(key.last_validated_at),
        }
        for key in keys
    ]


@router.post("/admin/ea-keys", status_code=201)
def create_ea_license_key(payload: EALicenseKeyCreate, db: Session = Depends(get_db)) -> dict[str, object]:
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="Nama key wajib diisi")

    # Semua masa berlaku ditafsirkan dalam waktu Indonesia Barat.
    # Date-only expiry remains active for the full selected WIB date.
    expiration_time = payload.expires_time if payload.expires_time is not None else time.max
    expires_at = datetime.combine(payload.expires_on, expiration_time, tzinfo=WIB).astimezone(timezone.utc)
    if expires_at <= utc_now():
        raise HTTPException(status_code=422, detail="Masa berlaku API key harus berada di waktu mendatang")

    raw_key = "ALG-" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    key = EALicenseKey(label=label, key_hash=key_hash, created_at=utc_now(), expires_at=expires_at)
    db.add(key)
    db.commit()
    db.refresh(key)
    return {
        "id": key.id,
        "label": key.label,
        "key": raw_key,
        "fingerprint": key_hash[-8:].upper(),
        "created_at": wib_iso(key.created_at),
        "expires_at": wib_iso(key.expires_at),
    }


@router.delete("/admin/ea-keys/{key_id}")
def delete_ea_license_key(key_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    key = db.get(EALicenseKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key tidak ditemukan")
    db.delete(key)
    db.commit()
    return {"status": "deleted"}


@router.get("/stats")
async def stats(db: Session = Depends(get_db)) -> dict[str, object]:
    payload: dict[str, object] = get_dashboard_stats(db)
    payload.update(risk_state(db))
    telegram = await telegram_manager.status()
    settings = get_settings()
    payload.update({
        "telegram_connected": bool(telegram["connected"]),
        "telegram_status": telegram["state"],
        "telegram_account": telegram["account_name"],
        "gemini_configured": bool(settings.gemini_api_key and settings.gemini_api_key.get_secret_value()),
        "gemini_model": settings.gemini_model,
    })
    return payload


@router.get("/signals", response_model=list[SignalResponse])
def signals(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, min_length=1, max_length=32),
    group_id: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    query = (
        select(Signal, Trade.ticket, Trade.profit)
        .outerjoin(Trade, Trade.signal_id == Signal.id)
        .where(or_(Signal.group_id.is_(None), ~Signal.group_id.like("ea-scalper:%")))
    )
    if status:
        query = query.where(Signal.status == status.upper())
    if group_id:
        query = query.where(Signal.group_id == group_id)
    query = query.order_by(Signal.created_at.desc(), Signal.id.desc()).offset(offset).limit(limit)
    items = []
    for signal, ticket, profit in db.execute(query).all():
        item = SignalResponse.model_validate(signal).model_dump()
        item["created_at"] = wib_iso(signal.created_at)
        item.update({"ticket": ticket, "profit": profit})
        items.append(item)
    return items


def _logs_query(level: str | None, search: str | None):
    query = select(SystemLog)
    if level:
        query = query.where(SystemLog.level == level.upper())
    if search:
        query = query.where(SystemLog.message.contains(search, autoescape=True))
    return query


def _csv_text(value: str | None) -> str:
    text = value or ""
    if text.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


@router.get("/logs", response_model=list[LogResponse])
def logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    level: str | None = Query(default=None, min_length=1, max_length=16),
    search: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    query = _logs_query(level, search).order_by(SystemLog.created_at.desc(), SystemLog.id.desc()).offset(offset).limit(limit)
    rows = db.execute(query).scalars()
    return [
        {**LogResponse.model_validate(row).model_dump(), "created_at": wib_iso(row.created_at)}
        for row in rows
    ]


@router.get("/logs/export.csv")
def export_logs(
    level: str | None = Query(default=None, max_length=16),
    search: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    rows = db.execute(_logs_query(level, search).order_by(SystemLog.created_at.desc()).limit(10000)).scalars()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "level", "source", "message"])
    for row in rows:
        writer.writerow([row.id, wib_iso(row.created_at), _csv_text(row.level), _csv_text(row.source), _csv_text(row.message)])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=logs.csv"})


@router.get("/groups/stats")
def group_stats(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return get_group_stats(db)


@router.get("/scalper/stats")
def scalper_stats(db: Session = Depends(get_db)) -> dict[str, object]:
    return get_scalper_stats(db)


def _trades_query(
    date_from: datetime | None,
    date_to: datetime | None,
    group_id: str | None,
    symbol: str | None,
    result: str | None,
):
    date_from = wib_datetime_to_utc_naive(date_from) if date_from else None
    date_to = wib_datetime_to_utc_naive(date_to) if date_to else None
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from harus sebelum atau sama dengan date_to")
    query = (
        select(Trade, Signal.group_id, Signal.group_name)
        .outerjoin(Signal, Signal.id == Trade.signal_id)
        .where(or_(Signal.id.is_(None), Signal.group_id.is_(None), ~Signal.group_id.like("ea-scalper:%")))
    )
    if date_from:
        query = query.where(Trade.opened_at >= date_from)
    if date_to:
        query = query.where(Trade.opened_at <= date_to)
    if group_id:
        query = query.where(Signal.group_id == group_id)
    if symbol:
        query = query.where(Trade.symbol == symbol.upper())
    if result:
        query = query.where(Trade.result == result.upper())
    return query.order_by(Trade.opened_at.desc(), Trade.id.desc())


def _trade_dict(trade: Trade, group_id: str | None, group_name: str | None) -> dict[str, object]:
    return {
        "id": trade.id,
        "source": "Telegram",
        "signal_id": trade.signal_id,
        "ticket": trade.ticket,
        "group_id": group_id,
        "group_name": group_name,
        "symbol": trade.symbol,
        "action": trade.action,
        "lots": trade.lots,
        "entry": trade.entry,
        "sl": trade.sl,
        "tp": trade.tp,
        "exec_price": trade.exec_price,
        "profit": trade.profit,
        "result": trade.result,
        "opened_at": wib_iso(trade.opened_at),
        "closed_at": wib_iso(trade.closed_at),
    }


def _scalper_trades_query(
    date_from: datetime | None,
    date_to: datetime | None,
    group_id: str | None,
    symbol: str | None,
    result: str | None,
):
    date_from = wib_datetime_to_utc_naive(date_from) if date_from else None
    date_to = wib_datetime_to_utc_naive(date_to) if date_to else None
    query = select(ScalperTrade)
    if date_from:
        query = query.where(ScalperTrade.opened_at >= date_from)
    if date_to:
        query = query.where(ScalperTrade.opened_at <= date_to)
    if group_id:
        prefix = "ea-scalper:"
        magic = group_id[len(prefix):] if group_id.startswith(prefix) else ""
        query = query.where(ScalperTrade.magic == int(magic)) if magic.isdigit() else query.where(ScalperTrade.id == -1)
    if symbol:
        query = query.where(ScalperTrade.symbol == symbol.upper())
    if result:
        query = query.where(ScalperTrade.result == result.upper())
    return query.order_by(ScalperTrade.opened_at.desc(), ScalperTrade.id.desc())


def _scalper_trade_dict(trade: ScalperTrade) -> dict[str, object]:
    return {
        "id": f"scalper-{trade.id}",
        "source": "EA Scalper",
        "signal_id": None,
        "ticket": trade.ticket,
        "group_id": f"ea-scalper:{trade.magic}",
        "group_name": f"EA Scalper ({trade.magic})",
        "symbol": trade.symbol,
        "action": trade.action,
        "lots": trade.lots,
        "entry": trade.entry,
        "sl": trade.sl,
        "tp": trade.tp,
        "exec_price": trade.entry,
        "profit": trade.profit,
        "result": trade.result,
        "opened_at": wib_iso(trade.opened_at),
        "closed_at": wib_iso(trade.closed_at),
    }


def _combined_trade_rows(
    db: Session,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    group_id: str | None,
    symbol: str | None,
    result: str | None,
    limit: int,
    offset: int = 0,
) -> list[dict[str, object]]:
    count = limit + offset
    telegram_rows = db.execute(
        _trades_query(date_from, date_to, group_id, symbol, result).limit(count)
    ).all()
    scalper_rows = db.execute(
        _scalper_trades_query(date_from, date_to, group_id, symbol, result).limit(count)
    ).scalars().all()
    combined = [
        _trade_dict(trade, group_id_value, group_name)
        for trade, group_id_value, group_name in telegram_rows
    ]
    combined.extend(_scalper_trade_dict(trade) for trade in scalper_rows)
    combined.sort(
        key=lambda row: (row["opened_at"] or "", row["source"], str(row["id"])),
        reverse=True,
    )
    return combined[offset:offset + limit]


@router.get("/trades")
def trades(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    group_id: str | None = Query(default=None, max_length=128),
    symbol: str | None = Query(default=None, max_length=64),
    result: str | None = Query(default=None, max_length=16),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return _combined_trade_rows(
        db,
        date_from=date_from,
        date_to=date_to,
        group_id=group_id,
        symbol=symbol,
        result=result,
        limit=limit,
        offset=offset,
    )


@router.get("/trades/export.csv")
def export_trades(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    group_id: str | None = Query(default=None, max_length=128),
    symbol: str | None = Query(default=None, max_length=64),
    result: str | None = Query(default=None, max_length=16),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    rows = _combined_trade_rows(
        db,
        date_from=date_from,
        date_to=date_to,
        group_id=group_id,
        symbol=symbol,
        result=result,
        limit=10000,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["source", "ticket", "group", "symbol", "action", "lots", "entry", "exec_price", "profit", "result", "opened_at", "closed_at"])
    for trade in rows:
        writer.writerow([
            _csv_text(trade["source"]), _csv_text(trade["ticket"]), _csv_text(trade["group_name"]),
            _csv_text(trade["symbol"]), _csv_text(trade["action"]), trade["lots"], trade["entry"],
            trade["exec_price"], trade["profit"], trade["result"], trade["opened_at"] or "", trade["closed_at"] or "",
        ])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=trades.csv"})
