from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.gemini_parser import SignalClassification
from app.main import app, migrate_legacy_scalper_trades
from app.models import AppSetting, EAExecution, EAStatus, ScalperEAStatus, ScalperTrade, Signal, SystemLog, Trade, utc_now


API_KEY = "m4-test-ea-api-key-32-characters-long"


def set_controls(**values):
    with SessionLocal() as db:
        for key, value in values.items():
            db.merge(AppSetting(key=key, value=json.dumps(value)))
        db.commit()


def parsed_signal(symbol="XAUUSD"):
    return SignalClassification(
        is_signal=True, type="NEW_SIGNAL", action="BUY", order_type="MARKET", symbol=symbol,
        entry=None, tp=[2450.0], sl=2400.0, confidence=0.95, reason="test",
    ).model_dump_json()


def test_scalper_reports_are_separate_and_visible_in_admin_history():
    headers = {"X-API-Key": API_KEY}
    common = {
        "position_id": "765432109",
        "terminal_login": "12345678",
        "terminal_server": "Broker-Demo",
        "magic": 990022,
        "symbol": "XAUUSD",
        "action": "BUY",
        "lots": 0.01,
        "price": 2450.25,
        "sl": 2440.0,
        "tp": 2472.0,
        "profit": 0.0,
    }

    try:
        with TestClient(app) as client:
            heartbeat = client.post("/api/ea/scalper/heartbeat", headers=headers, json={
                "active": True,
                "terminal_login": common["terminal_login"],
                "terminal_server": common["terminal_server"],
                "version": "EA Scalper 3.30",
                "symbol": common["symbol"],
                "magic": common["magic"],
            })
            assert heartbeat.status_code == 200

            opened = client.post("/api/ea/scalper/trade", headers=headers, json={**common, "event": "OPEN"})
            assert opened.status_code == 200
            duplicate_open = client.post("/api/ea/scalper/trade", headers=headers, json={**common, "event": "OPEN"})
            assert duplicate_open.status_code == 200
            assert duplicate_open.json()["duplicate"] is True

            closed = client.post("/api/ea/scalper/trade", headers=headers, json={
                **common,
                "event": "CLOSE",
                "profit": 12.5,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            })
            assert closed.status_code == 200
            duplicate_close = client.post("/api/ea/scalper/trade", headers=headers, json={
                **common,
                "event": "CLOSE",
                "profit": 12.5,
            })
            assert duplicate_close.status_code == 200
            assert duplicate_close.json()["duplicate"] is True

            login = client.post("/api/auth/login", json={
                "username": "admin",
                "password": "M1-Testing-Password-123",
            })
            assert login.status_code == 200
            stats = client.get("/api/scalper/stats")
            assert stats.status_code == 200
            assert stats.json()["ea_online"] is True
            assert stats.json()["trades_total"] == 1
            assert stats.json()["wins"] == 1
            assert stats.json()["losses"] == 0
            assert stats.json()["profit"] == 12.5

            trades = client.get("/api/trades?group_id=ea-scalper:990022")
            assert trades.status_code == 200
            [trade] = trades.json()
            assert trade["source"] == "EA Scalper"
            assert trade["result"] == "WIN"
            assert trade["group_name"] == "EA Scalper (990022)"
            groups = client.get("/api/groups/stats").json()
            assert all(not str(group.get("group_id", "")).startswith("ea-scalper:") for group in groups)
            assert all(not item.get("group_name", "").startswith("EA Scalper") for item in client.get("/api/signals").json())
            logs = client.get("/api/logs?search=765432109").json()
            assert len(logs) == 2
            assert all(item["source"] == "EA_SCALPER" for item in logs)
            assert "EA Scalper" in client.get("/api/trades/export.csv").text
    finally:
        with SessionLocal() as db:
            db.query(SystemLog).filter(
                SystemLog.source == "EA_SCALPER",
                SystemLog.message.contains("765432109"),
            ).delete(synchronize_session=False)
            db.query(ScalperTrade).filter_by(position_id="765432109").delete(synchronize_session=False)
            db.query(ScalperEAStatus).filter_by(id=1).delete(synchronize_session=False)
            db.commit()


def test_legacy_scalper_trade_is_moved_out_of_telegram_signal_tables():
    ticket = "SCALPER-112233445-abcdef123456"
    with SessionLocal() as db:
        signal = Signal(
            group_id="ea-scalper:990022",
            group_name="EA Scalper (990022)",
            raw_text="EA Scalper BUY XAUUSD",
            status="WIN",
            created_at=utc_now(),
        )
        db.add(signal)
        db.flush()
        db.add(Trade(
            signal_id=signal.id,
            ticket=ticket,
            symbol="XAUUSD",
            action="BUY",
            lots=0.01,
            entry=2450.0,
            tp="[2472.0]",
            exec_price=2450.0,
            profit=12.0,
            result="WIN",
            opened_at=utc_now(),
            closed_at=utc_now(),
        ))
        db.commit()
        signal_id = signal.id

    try:
        with SessionLocal() as db:
            migrate_legacy_scalper_trades(db)
            migrated = db.query(ScalperTrade).filter_by(ticket=ticket).one()
            assert migrated.magic == 990022
            assert migrated.profit == 12.0
            assert db.query(Trade).filter_by(ticket=ticket).one_or_none() is None
            assert db.get(Signal, signal_id) is None
    finally:
        with SessionLocal() as db:
            db.query(ScalperTrade).filter_by(ticket=ticket).delete(synchronize_session=False)
            db.query(Trade).filter_by(ticket=ticket).delete(synchronize_session=False)
            db.query(Signal).filter_by(id=signal_id).delete(synchronize_session=False)
            db.commit()


def test_ea_api_key_heartbeat_claim_report_and_trade_result():
    with SessionLocal() as db:
        for key, value in {
            "kill_switch": "false", "max_daily_loss_money": "100.0",
            "max_lot": "5.0", "max_open_trades": "3", "max_signal_age_seconds": "120",
            "max_market_deviation_pct": "5.0", "allowed_symbols": "[]",
        }.items():
            db.merge(AppSetting(key=key, value=value))
        db.commit()

    with TestClient(app) as client:
        assert client.get("/api/ea/pending").status_code == 401
        headers = {"X-API-Key": API_KEY}
        heartbeat = client.post("/api/ea/heartbeat", headers=headers, json={"active": True, "version": "test", "symbol": "XAUUSD"})
        assert heartbeat.status_code == 200
        assert heartbeat.json()["active"] is True

        parsed = SignalClassification(
            is_signal=True,
            type="NEW_SIGNAL",
            action="BUY",
            order_type="MARKET",
            symbol="XAUUSD",
            entry=None,
            tp=[2450.0],
            sl=2400.0,
            confidence=0.95,
            reason="mock signal",
        )
        with SessionLocal() as db:
            signal = Signal(
                group_id="-1001",
                group_name="Test group",
                message_id="101",
                raw_text="BUY XAUUSD",
                parsed_json=parsed.model_dump_json(),
                status="PENDING",
                created_at=utc_now(),
            )
            db.add(signal)
            db.commit()
            db.refresh(signal)
            signal_id = signal.id

        pending = client.get("/api/ea/pending?limit=1", headers=headers)
        assert pending.status_code == 200
        assert pending.json()["items"][0]["signal_id"] == signal_id
        assert "demo_mode" not in pending.json()
        assert client.get("/api/ea/pending", headers=headers).json()["items"] == []
        set_controls(demo_mode=True)

        report = {"signal_id": signal_id, "status": "EXECUTED", "ticket": "98765", "symbol": "XAUUSD", "action": "BUY", "lots": 0.02, "exec_price": 2425.0}
        assert client.post("/api/ea/report", headers=headers, json=report).status_code == 200
        duplicate = {**report, "status": "REJECTED"}
        assert client.post("/api/ea/report", headers=headers, json=duplicate).json()["duplicate"] is True

        result = {"ticket": "98765", "profit": 25.5, "result": "WIN", "closed_at": datetime.now(timezone.utc).isoformat()}
        assert client.post("/api/ea/result", headers=headers, json=result).status_code == 200
        assert client.post("/api/ea/result", headers=headers, json=result).json()["duplicate"] is True

        with SessionLocal() as db:
            stored_signal = db.get(Signal, signal_id)
            trade = db.query(Trade).filter_by(ticket="98765").one()
            ea_status = db.get(EAStatus, 1)
            assert stored_signal.status == "WIN"
            assert trade.profit == 25.5
            assert trade.result == "WIN"
            assert ea_status.active is True
        with SessionLocal() as db:
            db.query(EAExecution).filter_by(signal_id=signal_id).delete(synchronize_session=False)
            db.query(Trade).filter_by(ticket="98765").delete(synchronize_session=False)
            db.query(Signal).filter_by(id=signal_id).delete(synchronize_session=False)
            db.commit()


def test_ea_report_rejects_profit_result_sign_mismatch():
    with SessionLocal() as db:
        db.add(Trade(ticket="sign-mismatch"))
        db.commit()
    with TestClient(app) as client:
        assert client.post(
            "/api/ea/result",
            headers={"X-API-Key": API_KEY},
            json={"ticket": "sign-mismatch", "profit": -1, "result": "WIN"},
        ).status_code == 422


def test_ea_pending_enforces_kill_switch_expiry_and_xauusd_only():
    set_controls(
        kill_switch=True, max_daily_loss_money=0, max_lot=1,
        max_open_trades=3, max_market_deviation_pct=5,
    )
    now = utc_now()
    with SessionLocal() as db:
        stale = Signal(group_id="risk-test", message_id="stale", raw_text="old", parsed_json=parsed_signal("XAUUSD"), status="PENDING", created_at=now - timedelta(hours=2))
        forbidden = Signal(group_id="risk-test", message_id="forbidden", raw_text="wrong symbol", parsed_json=parsed_signal("EURUSD"), status="PENDING", created_at=now)
        db.add_all([stale, forbidden])
        db.commit()
        stale_id, forbidden_id = stale.id, forbidden.id

    try:
        with TestClient(app) as client:
            headers = {"X-API-Key": API_KEY}
            paused = client.get("/api/ea/pending", headers=headers)
            assert paused.status_code == 200
            assert paused.json()["items"] == []
            assert paused.json()["kill_switch"] is True
            set_controls(kill_switch=False)
            filtered = client.get("/api/ea/pending?limit=10", headers=headers)
            assert filtered.status_code == 200
            assert filtered.json()["items"] == []
            with SessionLocal() as db:
                assert db.get(Signal, stale_id).status == "REJECTED"
                assert db.get(Signal, forbidden_id).status == "REJECTED"
    finally:
        with SessionLocal() as db:
            db.query(Signal).filter(Signal.id.in_([stale_id, forbidden_id])).delete(synchronize_session=False)
            db.commit()
        set_controls(kill_switch=False, max_daily_loss_money=100, max_lot=5, max_open_trades=3, max_market_deviation_pct=5)


def test_pending_pins_single_entry_mode_for_execution_reports():
    set_controls(
        kill_switch=False, entry_mode="SINGLE",
        max_daily_loss_money=100, max_lot=5, max_open_trades=3,
        max_signal_age_seconds=120, max_market_deviation_pct=5, allowed_symbols=[],
    )
    parsed = SignalClassification(
        is_signal=True,
        type="NEW_SIGNAL",
        action="BUY",
        order_type="AUTO",
        symbol="XAUUSD",
        entry=None,
        entry_low=2400.0,
        entry_high=2410.0,
        tp=[2450.0, 2460.0],
        sl=2390.0,
        confidence=0.95,
        reason="test",
    )
    with SessionLocal() as db:
        signal = Signal(
            group_id="entry-mode-test",
            message_id="single-mode",
            raw_text="BUY XAUUSD 2400-2410",
            parsed_json=parsed.model_dump_json(),
            status="PENDING",
            created_at=utc_now(),
        )
        db.add(signal)
        db.commit()
        signal_id = signal.id

    try:
        with TestClient(app) as client:
            headers = {"X-API-Key": API_KEY}
            claimed = client.get("/api/ea/pending?limit=1", headers=headers)
            item = next(item for item in claimed.json()["items"] if item["signal_id"] == signal_id)
            assert item["entry_mode"] == "SINGLE"

            # A dashboard change must not alter the leg count of a signal already claimed.
            set_controls(entry_mode="PARTIAL")
            report = {
                "signal_id": signal_id,
                "status": "EXECUTED",
                "ticket": "single-entry-ticket",
                "symbol": "XAUUSD",
                "action": "BUY",
                "lots": 0.02,
                "exec_price": 2405.0,
                "leg": 0,
            }
            response = client.post("/api/ea/report", headers=headers, json=report)
            assert response.status_code == 200
    finally:
        with SessionLocal() as db:
            db.query(EAExecution).filter_by(signal_id=signal_id).delete(synchronize_session=False)
            db.query(Trade).filter_by(ticket="single-entry-ticket").delete(synchronize_session=False)
            db.query(Signal).filter_by(id=signal_id).delete(synchronize_session=False)
            db.commit()
        set_controls(
            kill_switch=False, entry_mode="PARTIAL",
            max_daily_loss_money=100, max_lot=5, max_open_trades=3,
            max_signal_age_seconds=120, max_market_deviation_pct=5, allowed_symbols=[],
        )


def test_legacy_demo_setting_does_not_block_live_report():
    with TestClient(app) as client:
        set_controls(demo_mode=True, kill_switch=False)
        with SessionLocal() as db:
            signal = Signal(
                group_id="legacy-demo-test",
                message_id="legacy-demo",
                raw_text="BUY XAUUSD",
                parsed_json=parsed_signal(),
                status="PENDING",
                created_at=utc_now(),
            )
            db.add(signal)
            db.commit()
            signal_id = signal.id

        headers = {"X-API-Key": API_KEY}
        claimed = client.get("/api/ea/pending?limit=1", headers=headers)
        assert claimed.status_code == 200
        assert claimed.json()["items"][0]["signal_id"] == signal_id
        report = {
            "signal_id": signal_id,
            "status": "EXECUTED",
            "ticket": "legacy-demo-ticket",
            "symbol": "XAUUSD",
            "action": "BUY",
            "lots": 0.01,
            "exec_price": 2425,
        }
        assert client.post("/api/ea/report", headers=headers, json=report).status_code == 200

        with SessionLocal() as db:
            db.query(EAExecution).filter_by(signal_id=signal_id).delete(synchronize_session=False)
            db.query(Trade).filter_by(ticket="legacy-demo-ticket").delete(synchronize_session=False)
            db.query(Signal).filter_by(id=signal_id).delete(synchronize_session=False)
            db.delete(db.get(AppSetting, "demo_mode"))
            db.commit()
