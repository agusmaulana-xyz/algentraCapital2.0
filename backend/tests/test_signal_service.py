import asyncio
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.gemini_parser import SignalClassification
from app.models import AppSetting, Signal
from app.signal_service import SignalService


class FixedParser:
    def __init__(self, classification):
        self.classification = classification

    async def parse(self, message, context=None):
        return self.classification


def classification(**updates):
    values = {
        "is_signal": True,
        "type": "NEW_SIGNAL",
        "action": "BUY",
        "order_type": "MARKET",
        "symbol": "GOLD",
        "entry": 2400.0,
        "tp": [2420.0],
        "sl": 2380.0,
        "confidence": 0.95,
        "reason": "mock signal",
    }
    values.update(updates)
    return SignalClassification.model_validate(values)


def test_process_message_normalizes_persists_and_deduplicates():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    timestamp = datetime(2026, 9, 30, 10, 15, tzinfo=timezone.utc)
    service = SignalService(FixedParser(classification()))

    with Session(engine, expire_on_commit=False) as db:
        db.add(AppSetting(key="symbol_mapping", value='{"GOLD":"XAUUSD"}'))
        db.commit()
        first = asyncio.run(service.process_message(
            db,
            group_id="-1001",
            group_name="Gold Signals",
            message_id="55",
            raw_text="Gold buy 2400 TP 2420 SL 2380",
            created_at=timestamp,
        ))
        duplicate = asyncio.run(service.process_message(
            db,
            group_id="-1001",
            group_name="Gold Signals",
            message_id="56",
            raw_text="Gold buy 2400 TP 2420 SL 2380",
            created_at=timestamp,
        ))

        row = db.get(Signal, first.signal_id)
        assert first.status == "PENDING"
        assert first.normalized_text == "BUY : ENTRY : 2400 | SL : 2380 | TP1 : 2420"
        assert row is not None and row.parsed_json is not None
        assert duplicate.duplicate is True
        assert duplicate.signal_id == first.signal_id
        assert db.query(Signal).count() == 1

    engine.dispose()


def test_process_message_rejects_inverted_prices_and_logs_ignored_messages():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    invalid_service = SignalService(FixedParser(classification(sl=2410.0)))
    ignored_service = SignalService(FixedParser(classification(is_signal=False, type="NOT_SIGNAL", action=None, order_type=None, symbol=None, entry=None, tp=None, sl=None)))

    with Session(engine, expire_on_commit=False) as db:
        invalid = asyncio.run(invalid_service.process_message(
            db, group_id="g", group_name="Group", message_id="1", raw_text="BUY 2400 SL 2410 TP 2420"
        ))
        ignored = asyncio.run(ignored_service.process_message(
            db, group_id="g", group_name="Group", message_id="2", raw_text="good morning"
        ))
        assert invalid.status == "REJECTED"
        assert ignored.status == "IGNORED"
        assert db.query(Signal).count() == 2

    engine.dispose()
