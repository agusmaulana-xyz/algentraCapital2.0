import hashlib
import hmac
import json
import math
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..gemini_parser import SignalClassification
from ..models import (
    AppSetting,
    EAExecution,
    EAStatus,
    MT5MasterEquityCandle,
    MT5MasterEquityMinuteSample,
    MT5MasterEquitySample,
    MasterCopyPosition,
    MasterCopyState,
    MT5MasterAccountState,
    MT5MasterMarketState,
    ScalperEAStatus,
    ScalperTrade,
    Signal,
    SystemLog,
    Trade,
    utc_now,
)
from ..public_realtime import public_performance_hub
from .public import public_performance_payload
from ..schemas import CopySnapshotInput
from ..risk_controls import risk_state
from ..time_utils import wib_iso


router = APIRouter(prefix="/api/ea", tags=["ea"])
FINAL_SIGNAL_STATES = {"EXECUTED", "REJECTED", "FAILED", "EXPIRED"}
CLAIM_LEASE_SECONDS = 90
MAX_SIGNAL_TARGETS = 20
SIGNAL_EXPIRY_SECONDS = 3600


class ScalperTradeReport(BaseModel):
    event: Literal["OPEN", "CLOSE"]
    position_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")
    terminal_login: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")
    terminal_server: str = Field(min_length=1, max_length=128)
    magic: int = Field(ge=0, le=9223372036854775807)
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    action: Literal["BUY", "SELL"]
    lots: float = Field(gt=0, le=1000, allow_inf_nan=False)
    price: float = Field(gt=0, allow_inf_nan=False)
    sl: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    tp: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    profit: float = Field(default=0.0, allow_inf_nan=False)
    closed_at: datetime | None = None


class ScalperHeartbeatReport(BaseModel):
    active: bool = True
    terminal_login: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")
    terminal_server: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=64)
    magic: int = Field(ge=0, le=9223372036854775807)


def require_ea_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    configured = get_settings().ea_api_key
    if configured is None or not configured.get_secret_value():
        raise HTTPException(status_code=503, detail="EA_API_KEY belum dikonfigurasi")
    if not x_api_key or not hmac.compare_digest(x_api_key, configured.get_secret_value()):
        raise HTTPException(status_code=401, detail="API key EA tidak valid")


@router.post("/scalper/trade", dependencies=[Depends(require_ea_key)])
def report_scalper_trade(payload: ScalperTradeReport, db: Session = Depends(get_db)) -> dict[str, object]:
    account_ref = hashlib.sha256(
        f"{payload.terminal_login}@{payload.terminal_server}".casefold().encode("utf-8")
    ).hexdigest()[:12]
    ticket = f"SCALPER-{payload.position_id}-{account_ref}"
    trade = db.execute(select(ScalperTrade).where(ScalperTrade.ticket == ticket)).scalar_one_or_none()
    if trade is not None and trade.closed_at is not None:
        return {"status": trade.result, "ticket": ticket, "duplicate": True}

    now = utc_now()
    is_new_trade = trade is None
    if is_new_trade:
        trade = ScalperTrade(
            ticket=ticket,
            position_id=payload.position_id,
            account_ref=account_ref,
            terminal_login=payload.terminal_login,
            terminal_server=payload.terminal_server,
            magic=payload.magic,
            symbol=payload.symbol.upper(),
            action=payload.action,
            lots=payload.lots,
            entry=payload.price,
            sl=payload.sl,
            tp=json.dumps([payload.tp]) if payload.tp is not None else "[]",
            opened_at=now,
        )
        db.add(trade)
    else:
        trade.lots = payload.lots
        trade.symbol = payload.symbol.upper()
        trade.action = payload.action
        trade.terminal_login = payload.terminal_login
        trade.terminal_server = payload.terminal_server
        if payload.event == "OPEN":
            trade.entry = payload.price
            trade.sl = payload.sl if payload.sl is not None else trade.sl
            if payload.tp is not None:
                trade.tp = json.dumps([payload.tp])

    if payload.event == "CLOSE":
        closed_at = payload.closed_at or now
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=now.tzinfo)
        trade.profit = payload.profit
        trade.result = "WIN" if payload.profit > 0 else "LOSS" if payload.profit < 0 else "BE"
        trade.closed_at = closed_at

    if not (not is_new_trade and payload.event == "OPEN"):
        description = (
            f"EA Scalper {payload.event.lower()}: {ticket} {payload.action} {payload.symbol}"
            if payload.event == "OPEN"
            else f"EA Scalper ditutup: {ticket}, {trade.result}, profit {trade.profit:g}"
        )
        db.add(SystemLog(
            level="INFO" if payload.event == "OPEN" or trade.result in {"WIN", "BE"} else "WARN",
            source="EA_SCALPER",
            message=description,
        ))

    db.commit()
    return {
        "status": trade.result if payload.event == "CLOSE" else "EXECUTED",
        "ticket": ticket,
        "duplicate": not is_new_trade and payload.event == "OPEN",
    }


@router.post("/scalper/heartbeat", dependencies=[Depends(require_ea_key)])
def scalper_heartbeat(payload: ScalperHeartbeatReport, db: Session = Depends(get_db)) -> dict[str, object]:
    status = db.get(ScalperEAStatus, 1)
    if status is None:
        status = ScalperEAStatus(id=1)
        db.add(status)
    status.active = payload.active
    status.last_heartbeat = utc_now()
    status.terminal_login = payload.terminal_login
    status.terminal_server = payload.terminal_server
    status.version = payload.version
    status.symbol = payload.symbol
    status.magic = payload.magic
    db.commit()
    return {"status": "ok", "active": status.active, "last_heartbeat": wib_iso(status.last_heartbeat)}


@router.post("/master/snapshot", dependencies=[Depends(require_ea_key)])
def publish_master_snapshot(payload: CopySnapshotInput, db: Session = Depends(get_db)) -> dict[str, object]:
    ticket_values = [position.ticket for position in payload.positions]
    if len(ticket_values) != len(set(ticket_values)):
        raise HTTPException(status_code=422, detail="Ticket posisi harus unik")

    now = utc_now()
    db.execute(delete(MasterCopyPosition))
    for position in payload.positions:
        db.add(MasterCopyPosition(
            source_ticket=position.ticket,
            symbol=position.symbol,
            action=position.action,
            lots=position.lots,
            entry_price=position.entry_price,
            sl=position.sl,
            tp=position.tp,
            is_open=True,
            updated_at=now,
        ))

    state = db.get(MasterCopyState, 1)
    if state is None:
        state = MasterCopyState(id=1)
        db.add(state)
    state.last_snapshot_at = now
    master_report_fields = (
        payload.balance,
        payload.equity,
        payload.floating_profit,
        payload.currency,
        payload.trade_mode,
    )
    if all(value is not None for value in master_report_fields):
        account_state = db.get(MT5MasterAccountState, 1)
        if account_state is None:
            account_state = MT5MasterAccountState(id=1)
            db.add(account_state)
        account_state.balance = payload.balance
        account_state.equity = payload.equity
        account_state.floating_profit = payload.floating_profit
        account_state.currency = payload.currency.upper()
        account_state.trade_mode = payload.trade_mode
        account_state.observed_at = now

        sample_hour = now.replace(minute=0, second=0, microsecond=0)
        equity_sample = db.execute(
            select(MT5MasterEquitySample).where(MT5MasterEquitySample.sample_hour == sample_hour)
        ).scalar_one_or_none()
        if equity_sample is None:
            db.add(MT5MasterEquitySample(
                sample_hour=sample_hour,
                equity=payload.equity,
                currency=payload.currency.upper(),
                observed_at=now,
            ))
            db.execute(delete(MT5MasterEquitySample).where(
                MT5MasterEquitySample.sample_hour < now - timedelta(days=45),
            ))
        else:
            observed_at = equity_sample.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=now.tzinfo)
            if (now - observed_at).total_seconds() >= 60:
                equity_sample.equity = payload.equity
                equity_sample.currency = payload.currency.upper()
                equity_sample.observed_at = now

        sample_minute = now.replace(second=0, microsecond=0)
        minute_sample = db.execute(
            select(MT5MasterEquityMinuteSample).where(
                MT5MasterEquityMinuteSample.sample_minute == sample_minute,
            )
        ).scalar_one_or_none()
        if minute_sample is None:
            db.add(MT5MasterEquityMinuteSample(
                sample_minute=sample_minute,
                equity=payload.equity,
                currency=payload.currency.upper(),
                observed_at=now,
            ))
            db.execute(delete(MT5MasterEquityMinuteSample).where(
                MT5MasterEquityMinuteSample.sample_minute < now - timedelta(hours=48),
            ))
        else:
            observed_at = minute_sample.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=now.tzinfo)
            if (now - observed_at).total_seconds() >= 15:
                minute_sample.equity = payload.equity
                minute_sample.currency = payload.currency.upper()
                minute_sample.observed_at = now

        minute_start = now.replace(second=0, microsecond=0)
        account_key = f"{payload.terminal_login.strip()}@{payload.terminal_server.strip()}".upper()
        equity_candle = db.execute(
            select(MT5MasterEquityCandle).where(
                MT5MasterEquityCandle.account_key == account_key,
                MT5MasterEquityCandle.minute_start == minute_start,
            )
        ).scalar_one_or_none()
        if equity_candle is None:
            db.add(MT5MasterEquityCandle(
                account_key=account_key,
                minute_start=minute_start,
                open_equity=payload.equity,
                high_equity=payload.equity,
                low_equity=payload.equity,
                close_equity=payload.equity,
                currency=payload.currency.upper(),
                observed_at=now,
            ))
            db.execute(delete(MT5MasterEquityCandle).where(
                MT5MasterEquityCandle.minute_start < now - timedelta(days=45),
            ))
        else:
            equity_candle.high_equity = max(equity_candle.high_equity, payload.equity)
            equity_candle.low_equity = min(equity_candle.low_equity, payload.equity)
            equity_candle.close_equity = payload.equity
            equity_candle.currency = payload.currency.upper()
            equity_candle.observed_at = now

    if payload.market_quotes is not None:
        market_state = db.get(MT5MasterMarketState, 1)
        if market_state is None:
            market_state = MT5MasterMarketState(id=1)
            db.add(market_state)
        market_state.quotes_json = json.dumps(
            [quote.model_dump() for quote in payload.market_quotes],
            separators=(",", ":"),
        )
        market_state.observed_at = now
    db.commit()
    if public_performance_hub.connected:
        live_payload = public_performance_payload(db, include_equity_curve=False)
        if public_performance_hub.history_refresh_due():
            live_payload = public_performance_payload(db)
            public_performance_hub.mark_history_published()
        public_performance_hub.publish(live_payload)
    return {"status": "ok", "positions": len(payload.positions), "last_snapshot_at": wib_iso(now)}


class ExecutionReport(BaseModel):
    signal_id: int = Field(gt=0)
    status: Literal["EXECUTED", "REJECTED", "FAILED"]
    ticket: str | int | None = None
    symbol: str | None = Field(default=None, max_length=64)
    action: Literal["BUY", "SELL"] | None = None
    lots: float | None = Field(default=None, ge=0, le=1000)
    exec_price: float | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=1000)
    leg: int | None = Field(default=None, ge=0, le=MAX_SIGNAL_TARGETS)

    @model_validator(mode="after")
    def validate_execution_fields(self):
        ticket = str(self.ticket).strip() if self.ticket is not None else ""
        if self.status == "EXECUTED" and ticket in {"", "0"}:
            raise ValueError("Ticket wajib diisi untuk status EXECUTED")
        if self.lots is not None and not math.isfinite(self.lots):
            raise ValueError("Lot harus finite")
        if self.exec_price is not None and not math.isfinite(self.exec_price):
            raise ValueError("Harga eksekusi harus finite")
        if self.status == "EXECUTED":
            if self.lots is not None and self.lots <= 0:
                raise ValueError("Lot harus lebih besar dari 0 untuk status EXECUTED")
            if self.exec_price is not None and self.exec_price <= 0:
                raise ValueError("Harga eksekusi harus lebih besar dari 0 untuk status EXECUTED")
        else:
            # Older EA builds report zero values when no order was sent.
            if self.ticket is not None and ticket in {"", "0"}:
                self.ticket = None
            if self.lots == 0:
                self.lots = None
            if self.exec_price == 0:
                self.exec_price = None
        return self


class TradeResultReport(BaseModel):
    ticket: str | int
    profit: float
    result: Literal["WIN", "LOSS", "BE", "EXPIRED"]
    closed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_result(self):
        if not math.isfinite(self.profit):
            raise ValueError("Profit harus finite")
        return self


class HeartbeatReport(BaseModel):
    active: bool = True
    terminal: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=64)


def _entry_mode(db: Session) -> str:
    setting = db.get(AppSetting, "entry_mode")
    if setting is None:
        return "PARTIAL"
    try:
        value = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        return "PARTIAL"
    return value if isinstance(value, str) and value in {"SINGLE", "PARTIAL"} else "PARTIAL"


def _take_profit_prices(parsed: dict) -> list[float]:
    raw_targets = parsed.get("tp")
    if not isinstance(raw_targets, list):
        return []
    targets = [
        float(value)
        for value in raw_targets
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0
    ]
    return targets[:MAX_SIGNAL_TARGETS]


def _expected_signal_legs(parsed: dict) -> int:
    if parsed.get("execution_mode") == "SINGLE":
        return 1
    targets = _take_profit_prices(parsed)
    if targets:
        return len(targets)
    if parsed.get("entry_low") is not None and parsed.get("entry_high") is not None:
        return 2
    return 1


def _sync_signal_execution_status(db: Session, signal: Signal, expected_legs: int) -> None:
    executions = list(db.execute(
        select(EAExecution).where(EAExecution.signal_id == signal.id)
    ).scalars())
    expected = {0} if expected_legs == 1 else set(range(1, expected_legs + 1))
    by_leg = {execution.leg: execution for execution in executions}
    if not expected.issubset(by_leg):
        signal.status = "CLAIMED"
        return

    leg_statuses = [by_leg[leg].status for leg in expected]
    if "EXECUTED" not in leg_statuses:
        signal.status = "FAILED" if "FAILED" in leg_statuses else "REJECTED"
        return

    open_trade = db.execute(
        select(Trade.id).where(Trade.signal_id == signal.id, Trade.closed_at.is_(None)).limit(1)
    ).scalar_one_or_none()
    if open_trade is not None:
        signal.status = "EXECUTED"
        return

    results = list(db.execute(
        select(Trade.result, Trade.profit).where(Trade.signal_id == signal.id)
    ).all())
    if results and all(result == "EXPIRED" for result, _ in results):
        signal.status = "EXPIRED"
    elif results:
        total_profit = sum(profit for _, profit in results)
        signal.status = "BE" if abs(total_profit) < 0.005 else "WIN" if total_profit > 0 else "LOSS"
    else:
        signal.status = "EXECUTED"


@router.get("/pending", dependencies=[Depends(require_ea_key)])
def pending(
    limit: int = Query(default=1, ge=1, le=10),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    now = utc_now()
    expired_lease = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
    controls = risk_state(db)
    response_controls = {
        key: controls[key]
        for key in (
            "kill_switch", "daily_loss", "open_trades",
            "max_market_deviation_pct",
        )
    }
    if controls["trading_paused"]:
        return {"items": [], **response_controls, "server_time": wib_iso(now)}

    eligible = or_(
        Signal.status == "PENDING",
        and_(Signal.status == "CLAIMED", or_(Signal.claimed_at.is_(None), Signal.claimed_at < expired_lease)),
    )
    candidate_ids = list(db.execute(
        select(Signal.id).where(eligible).order_by(Signal.created_at.asc(), Signal.id.asc()).limit(limit)
    ).scalars())
    if not candidate_ids:
        return {"items": [], **response_controls, "server_time": wib_iso(now)}

    # Conditional update makes claiming safe when multiple EA clients poll together.
    db.execute(
        update(Signal).where(Signal.id.in_(candidate_ids), eligible).values(status="CLAIMED", claimed_at=now)
    )
    rows = list(db.execute(
        select(Signal).where(Signal.id.in_(candidate_ids), Signal.status == "CLAIMED", Signal.claimed_at == now)
    ).scalars())
    items = []
    for row in rows:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=now.tzinfo)
        if (now - created_at).total_seconds() > SIGNAL_EXPIRY_SECONDS:
            row.status = "REJECTED"
            db.add(SystemLog(level="WARN", source="EA", message=f"Signal {row.id} ditolak karena kedaluwarsa"))
            continue
        try:
            parsed_data = json.loads(row.parsed_json or "{}")
            parsed = SignalClassification.model_validate(parsed_data)
        except Exception:
            row.status = "FAILED"
            db.add(SystemLog(level="ERROR", source="EA", message=f"Signal {row.id} memiliki parsed_json tidak valid"))
            continue
        symbol = (parsed.symbol or "XAUUSD").upper()
        if not symbol.startswith("XAUUSD"):
            row.status = "REJECTED"
            db.add(SystemLog(level="WARN", source="EA", message=f"Signal {row.id} ditolak karena hanya XAUUSD yang diproses"))
            continue
        execution_mode = parsed_data.get("execution_mode")
        if not isinstance(execution_mode, str) or execution_mode not in {"SINGLE", "PARTIAL"}:
            execution_mode = _entry_mode(db)
            parsed_data["execution_mode"] = execution_mode
            row.parsed_json = json.dumps(parsed_data, ensure_ascii=False, allow_nan=False)
        items.append({
            "signal_id": row.id,
            "group_id": row.group_id,
            "group_name": row.group_name,
            "raw_text": row.raw_text,
            "action": parsed.action,
            "order_type": parsed.order_type,
            "symbol": parsed.symbol or "XAUUSD",
            "entry": parsed.entry,
            "entry_low": parsed.entry_low,
            "entry_high": parsed.entry_high,
            "sl": parsed.sl,
            "tp": parsed.tp or [],
            "entry_mode": execution_mode,
            "confidence": parsed.confidence,
            "created_at": wib_iso(row.created_at),
            # SQLite returns naive datetimes for UTC values; normalize before
            # converting to Unix time so the host's local timezone is ignored.
            "created_epoch": int(created_at.timestamp()),
        })
    db.commit()
    return {"items": items, **response_controls, "server_time": wib_iso(now)}


@router.post("/report", dependencies=[Depends(require_ea_key)])
def report(payload: ExecutionReport, db: Session = Depends(get_db)) -> dict[str, object]:
    signal = db.get(Signal, payload.signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal tidak ditemukan")
    parsed = {}
    try:
        parsed = json.loads(signal.parsed_json or "{}")
    except json.JSONDecodeError:
        pass
    expected_legs = _expected_signal_legs(parsed)
    leg = payload.leg if payload.leg is not None else 0
    multi_leg_report = expected_legs > 1 and 1 <= leg <= expected_legs
    if payload.status == "EXECUTED" and (
        (expected_legs > 1 and not multi_leg_report) or (expected_legs == 1 and leg != 0)
    ):
        raise HTTPException(status_code=422, detail="Nomor entry tidak sesuai jumlah TP pada signal")

    execution = db.execute(
        select(EAExecution).where(EAExecution.signal_id == signal.id, EAExecution.leg == leg)
    ).scalar_one_or_none()
    if execution is not None and execution.status == payload.status:
        if payload.status != "EXECUTED" or str(execution.ticket) == str(payload.ticket):
            return {"status": signal.status, "duplicate": True}
    if execution is not None and execution.status == "EXECUTED" and payload.status != "EXECUTED":
        return {"status": signal.status, "duplicate": True}

    is_execution = payload.status == "EXECUTED"
    if signal.status in FINAL_SIGNAL_STATES:
        can_upgrade_failed_leg = is_execution and execution is not None and execution.status in {"FAILED", "REJECTED"}
        if not can_upgrade_failed_leg:
            return {"status": signal.status, "duplicate": True}
    elif signal.status != "CLAIMED":
        raise HTTPException(status_code=409, detail="Signal belum diklaim oleh EA")
    ticket = str(payload.ticket) if payload.ticket is not None else None
    if is_execution and ticket:
        trade = db.execute(select(Trade).where(Trade.ticket == ticket)).scalar_one_or_none()
        if trade is not None and trade.signal_id != signal.id:
            raise HTTPException(status_code=409, detail="Ticket sudah terhubung ke signal lain")
        if execution is None:
            execution = EAExecution(signal_id=signal.id, leg=leg, status=payload.status)
            db.add(execution)
        execution.status = payload.status
        execution.ticket = ticket
        execution.lots = payload.lots
        execution.exec_price = payload.exec_price
        execution.reason = payload.reason
        if trade is None:
            trade = Trade(ticket=ticket, signal_id=signal.id, opened_at=utc_now())
            db.add(trade)
        trade.symbol = payload.symbol or parsed.get("symbol")
        trade.action = payload.action or parsed.get("action")
        trade.entry = parsed.get("entry") if parsed.get("entry") is not None else payload.exec_price
        trade.sl = parsed.get("sl")
        take_profits = _take_profit_prices(parsed)
        if expected_legs > 1:
            take_profits = [take_profits[leg - 1]] if leg <= len(take_profits) else []
        trade.tp = json.dumps(take_profits)
        trade.lots = payload.lots
        trade.exec_price = payload.exec_price
        db.flush()
        _sync_signal_execution_status(db, signal, expected_legs)
    elif multi_leg_report:
        if execution is None:
            execution = EAExecution(signal_id=signal.id, leg=leg, status=payload.status)
            db.add(execution)
        execution.status = payload.status
        execution.ticket = None
        execution.lots = payload.lots
        execution.exec_price = payload.exec_price
        execution.reason = payload.reason
        db.flush()
        _sync_signal_execution_status(db, signal, expected_legs)
    else:
        signal.status = payload.status
    db.add(SystemLog(
        level="INFO" if payload.status == "EXECUTED" else "WARN",
        source="EA",
        message=f"Signal {signal.id}: {payload.status}" + (f" — {payload.reason}" if payload.reason else ""),
    ))
    db.commit()
    return {"status": payload.status, "signal_id": signal.id, "leg": leg, "duplicate": False}


@router.post("/result", dependencies=[Depends(require_ea_key)])
def result(payload: TradeResultReport, db: Session = Depends(get_db)) -> dict[str, object]:
    ticket = str(payload.ticket)
    trade = db.execute(select(Trade).where(Trade.ticket == ticket)).scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade/ticket tidak ditemukan")
    if trade.closed_at is not None:
        return {"status": trade.result, "duplicate": True}
    if (payload.result == "WIN" and payload.profit <= 0) or (payload.result == "LOSS" and payload.profit >= 0):
        raise HTTPException(status_code=422, detail="Hasil trade tidak sesuai tanda profit")
    trade.profit = payload.profit
    trade.result = payload.result
    trade.closed_at = payload.closed_at or utc_now()
    db.flush()
    if trade.signal_id is not None:
        signal = db.get(Signal, trade.signal_id)
        if signal is not None:
            parsed = {}
            try:
                parsed = json.loads(signal.parsed_json or "{}")
            except json.JSONDecodeError:
                pass
            expected_legs = _expected_signal_legs(parsed)
            execution_count = db.execute(
                select(EAExecution.id).where(EAExecution.signal_id == signal.id).limit(1)
            ).scalar_one_or_none()
            if execution_count is not None:
                _sync_signal_execution_status(db, signal, expected_legs)
            else:
                open_ticket = db.execute(
                    select(Trade.id).where(Trade.signal_id == signal.id, Trade.closed_at.is_(None)).limit(1)
                ).scalar_one_or_none()
                if open_ticket is None:
                    results = list(db.execute(
                        select(Trade.result, Trade.profit).where(Trade.signal_id == signal.id)
                    ).all())
                    if results and all(result == "EXPIRED" for result, _ in results):
                        signal.status = "EXPIRED"
                    else:
                        total_profit = sum(profit for _, profit in results)
                        signal.status = "BE" if abs(total_profit) < 0.005 else "WIN" if total_profit > 0 else "LOSS"
    db.add(SystemLog(level="INFO", source="EA", message=f"Ticket {ticket} ditutup: {payload.result}, profit {payload.profit:g}"))
    db.commit()
    if public_performance_hub.connected:
        live_payload = public_performance_payload(db, include_equity_curve=False)
        live_payload["pnl_updated"] = True
        public_performance_hub.publish(live_payload)
    return {"status": payload.result, "ticket": ticket, "duplicate": False}


@router.post("/heartbeat", dependencies=[Depends(require_ea_key)])
def heartbeat(payload: HeartbeatReport, db: Session = Depends(get_db)) -> dict[str, object]:
    status = db.get(EAStatus, 1)
    if status is None:
        status = EAStatus(id=1)
        db.add(status)
    status.active = payload.active
    status.last_heartbeat = utc_now()
    status.terminal = payload.terminal
    status.version = payload.version
    status.symbol = payload.symbol
    db.commit()
    return {"status": "ok", "active": status.active, "last_heartbeat": wib_iso(status.last_heartbeat)}
