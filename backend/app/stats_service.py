from collections import Counter
from collections.abc import Iterable
from typing import Any

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import ClientUser, EAStatus, ScalperEAStatus, ScalperTrade, Signal, Trade
from .time_utils import as_utc, wib_iso


def calculate_trade_stats(trades: Iterable[Any]) -> dict[str, int | float]:
    wins = 0
    losses = 0
    total_profit = 0.0
    for trade in trades:
        result = (getattr(trade, "result", None) or "").upper()
        profit = getattr(trade, "profit", 0.0)
        total_profit += float(profit or 0.0)
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1

    decided_trades = wins + losses
    winrate = (wins / decided_trades * 100.0) if decided_trades else 0.0
    return {
        "wins": wins,
        "losses": losses,
        "winrate": round(winrate, 2),
        "total_profit": round(total_profit, 2),
    }


def get_dashboard_stats(db: Session) -> dict[str, int | float]:
    telegram_signal = or_(Signal.group_id.is_(None), ~Signal.group_id.like("ea-scalper:%"))
    signal_rows = db.execute(select(Signal.status).where(telegram_signal)).scalars().all()
    status_counts = Counter(status.upper() for status in signal_rows)
    telegram_trades = db.execute(
        select(Trade)
        .outerjoin(Signal, Signal.id == Trade.signal_id)
        .where(or_(Signal.id.is_(None), Signal.group_id.is_(None), ~Signal.group_id.like("ea-scalper:%")))
    ).scalars()
    trade_stats = calculate_trade_stats(telegram_trades)
    active_clients = db.execute(select(func.count(ClientUser.id))).scalar_one()
    ea = db.get(EAStatus, 1)
    heartbeat = ea.last_heartbeat if ea else None
    if heartbeat is not None:
        heartbeat = as_utc(heartbeat)
        ea_online = bool(ea.active and (datetime.now(timezone.utc) - heartbeat).total_seconds() <= 90)
    else:
        ea_online = False
    return {
        "signals_total": sum(status_counts.values()),
        "signals_executed": status_counts["EXECUTED"],
        "signals_pending": status_counts["PENDING"] + status_counts["CLAIMED"],
        "signals_rejected": status_counts["REJECTED"] + status_counts["FAILED"],
        "signals_ignored": status_counts["IGNORED"],
        "active_clients": active_clients,
        "ea_online": ea_online,
        "ea_last_heartbeat": wib_iso(heartbeat),
        **trade_stats,
    }


def get_group_stats(db: Session) -> list[dict[str, int | float | str | None]]:
    telegram_signal = or_(Signal.group_id.is_(None), ~Signal.group_id.like("ea-scalper:%"))
    signal_counts = db.execute(
        select(Signal.group_id, Signal.group_name, func.count(Signal.id))
        .where(telegram_signal)
        .group_by(Signal.group_id, Signal.group_name)
    ).all()
    trades = db.execute(
        select(Signal.group_id, Trade.result, Trade.profit)
        .join(Trade, Trade.signal_id == Signal.id)
        .where(telegram_signal)
    ).all()
    grouped: dict[str | None, dict[str, float | int]] = {}
    for group_id, result, profit in trades:
        stats = grouped.setdefault(group_id, {"wins": 0, "losses": 0, "profit": 0.0})
        result = (result or "").upper()
        if result == "WIN":
            stats["wins"] += 1
        elif result == "LOSS":
            stats["losses"] += 1
        stats["profit"] += float(profit or 0.0)

    output = []
    for group_id, group_name, total in signal_counts:
        stats = grouped.get(group_id, {"wins": 0, "losses": 0, "profit": 0.0})
        decided = int(stats["wins"]) + int(stats["losses"])
        output.append({
            "group_id": group_id,
            "group_name": group_name or "Unknown",
            "signals_total": total,
            "wins": int(stats["wins"]),
            "losses": int(stats["losses"]),
            "winrate": round(int(stats["wins"]) / decided * 100.0, 2) if decided else 0.0,
            "profit": round(float(stats["profit"]), 2),
        })
    return sorted(output, key=lambda row: row["signals_total"], reverse=True)


def get_scalper_stats(db: Session) -> dict[str, Any]:
    status = db.get(ScalperEAStatus, 1)
    heartbeat = as_utc(status.last_heartbeat) if status and status.last_heartbeat else None
    now = datetime.now(timezone.utc)
    online = bool(status and status.active and heartbeat and (now - heartbeat).total_seconds() <= 90)
    trades = db.execute(
        select(ScalperTrade).order_by(ScalperTrade.opened_at.desc(), ScalperTrade.id.desc())
    ).scalars().all()

    grouped: dict[int, dict[str, int | float]] = {}
    totals: dict[str, int | float] = {
        "trades_total": 0,
        "closed_trades": 0,
        "open_positions": 0,
        "wins": 0,
        "losses": 0,
        "break_even": 0,
        "profit": 0.0,
    }
    for trade in trades:
        group = grouped.setdefault(trade.magic, {
            "trades_total": 0,
            "closed_trades": 0,
            "open_positions": 0,
            "wins": 0,
            "losses": 0,
            "break_even": 0,
            "profit": 0.0,
        })
        for stats in (group, totals):
            stats["trades_total"] = int(stats["trades_total"]) + 1
            if trade.closed_at is None:
                stats["open_positions"] = int(stats["open_positions"]) + 1
            else:
                stats["closed_trades"] = int(stats["closed_trades"]) + 1
                stats["profit"] = float(stats["profit"]) + trade.profit
                if trade.result == "WIN":
                    stats["wins"] = int(stats["wins"]) + 1
                elif trade.result == "LOSS":
                    stats["losses"] = int(stats["losses"]) + 1
                elif trade.result == "BE":
                    stats["break_even"] = int(stats["break_even"]) + 1

    groups = []
    for magic, stats in sorted(grouped.items()):
        decided = int(stats["wins"]) + int(stats["losses"])
        groups.append({
            "group_id": f"ea-scalper:{magic}",
            "group_name": f"EA Scalper ({magic})",
            **stats,
            "winrate": round(int(stats["wins"]) / decided * 100, 2) if decided else 0.0,
            "profit": round(float(stats["profit"]), 2),
        })

    total_decided = int(totals["wins"]) + int(totals["losses"])
    return {
        **totals,
        "wins": int(totals["wins"]),
        "losses": int(totals["losses"]),
        "break_even": int(totals["break_even"]),
        "profit": round(float(totals["profit"]), 2),
        "winrate": round(int(totals["wins"]) / total_decided * 100, 2) if total_decided else 0.0,
        "ea_online": online,
        "ea_last_heartbeat": wib_iso(heartbeat),
        "ea_symbol": status.symbol if status else None,
        "ea_magic": status.magic if status else None,
        "ea_version": status.version if status else None,
        "groups": groups,
    }
