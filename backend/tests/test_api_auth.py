from datetime import timedelta
from pathlib import Path
from queue import Queue
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import hash_password, seed_admin, verify_password
from app.config import Settings
from app.database import Base, SessionLocal
from app.models import AdminUser, AuthSession, ClientUser, EAExecution, MT5Account, MT5AccountState, PasswordResetCode, Signal, Trade, utc_now
from app.routers.dashboard import _csv_text
from app.routers import auth as auth_router
from app.main import PROJECT_ROOT, app
from app.whatsapp_otp import WhatsAppOtpManager, _register_qr_callback, _send_otp_message


def test_qris_endpoint_serves_current_payment_image():
    with TestClient(app) as client:
        response = client.get("/api/payments/qris.jpeg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/jpeg")
        assert response.content.startswith(b"\xff\xd8\xff")


def test_admin_login_protects_and_unlocks_dashboard_routes():
    with TestClient(app) as client:
        assert client.get("/api/stats").status_code == 401
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "M1-Testing-Password-123"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "authenticated"
        csrf = response.json()["csrf_token"]
        old_cookie = response.headers["set-cookie"].split(";", 1)[0]
        cookie_value = old_cookie.split("=", 1)[1]
        assert "admin" not in cookie_value
        assert client.get("/parser-test").status_code == 200
        assert client.get("/whatsapp-otp-setup").status_code == 200
        whatsapp_status = client.get("/api/whatsapp-otp/status")
        assert whatsapp_status.status_code == 200
        assert "qr_data_uri" in whatsapp_status.json()
        assert client.post("/api/whatsapp-otp/disconnect").status_code == 403
        assert client.post("/api/whatsapp-otp/new-qr").status_code == 403
        assert client.post(
            "/api/whatsapp-otp/disconnect",
            headers={"X-CSRF-Token": csrf},
        ).status_code == 200
        assert client.get("/api/stats").json()["winrate"] == 0.0
        assert client.post("/api/auth/logout").status_code == 403
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.get("/api/stats").status_code == 401
        assert client.get("/api/stats", headers={"Cookie": old_cookie}).status_code == 401


def test_password_reset_request_does_not_disclose_account_existence(monkeypatch):
    monkeypatch.setattr(auth_router, "send_verification_code", lambda *_args, **_kwargs: None)
    with TestClient(app) as client:
        with SessionLocal() as db:
            db.add(ClientUser(
                email="privacy-check@example.com",
                password_hash=hash_password("A-Strong-Test-Password-123"),
                verified_at=utc_now(),
                email_verified_at=utc_now(),
            ))
            db.commit()
        known = client.post("/api/auth/password-reset/request", json={"email": "privacy-check@example.com"})
        unknown = client.post("/api/auth/password-reset/request", json={"email": "not-registered@example.com"})
        assert known.status_code == unknown.status_code == 202
        assert known.json() == unknown.json()


def test_whatsapp_registration_verifies_only_the_selected_contact(monkeypatch):
    sent_codes = []
    monkeypatch.setattr(WhatsAppOtpManager, "is_connected", property(lambda _self: True))
    monkeypatch.setattr(
        auth_router.whatsapp_otp_manager,
        "send_otp",
        lambda _phone, code, _purpose: sent_codes.append(code),
    )
    email = f"whatsapp-register-{uuid4().hex}@example.com"
    with TestClient(app) as client:
        registered = client.post("/api/auth/register", json={
            "email": email,
            "password": "A-Strong-Test-Password-123",
            "channel": "whatsapp",
            "phone": "+62 812-3456-7890",
        })
        assert registered.status_code == 202
        assert len(sent_codes) == 1

        verified = client.post("/api/auth/verify-otp", json={"email": email, "code": sent_codes[0]})
        assert verified.status_code == 200

    with SessionLocal() as db:
        user = db.query(ClientUser).filter_by(email=email).one()
        assert user.phone == "6281234567890"
        assert user.phone_verified_at is not None
        assert user.email_verified_at is None


def test_whatsapp_password_reset_requires_and_uses_verified_phone(monkeypatch):
    sent_codes = []
    monkeypatch.setattr(
        auth_router.whatsapp_otp_manager,
        "send_otp",
        lambda _phone, code, _purpose: sent_codes.append(code),
    )
    email = f"whatsapp-reset-{uuid4().hex}@example.com"
    phone = f"628{uuid4().int % 10**10:010d}"
    password = "Original-Password-123"
    with TestClient(app) as client:
        with SessionLocal() as db:
            user = ClientUser(
                email=email,
                phone=phone,
                phone_verified_at=utc_now(),
                email_verified_at=None,
                password_hash=hash_password(password),
                verified_at=utc_now(),
            )
            db.add(user)
            db.commit()

        login = client.post("/api/auth/client-login", json={"email": email, "password": password})
        assert login.status_code == 200
        old_cookie = login.headers["set-cookie"].split(";", 1)[0]

        email_reset = client.post("/api/auth/password-reset/request", json={"email": email, "channel": "email"})
        assert email_reset.status_code == 202
        assert sent_codes == []

        requested = client.post("/api/auth/password-reset/request", json={"email": email, "channel": "whatsapp"})
        assert requested.status_code == 202
        assert len(sent_codes) == 1
        confirmed = client.post("/api/auth/password-reset/confirm", json={
            "email": email,
            "channel": "whatsapp",
            "code": sent_codes[0],
            "password": "New-Strong-Password-456",
        })
        assert confirmed.status_code == 200
        assert client.get("/api/mt5/accounts", headers={"Cookie": old_cookie}).status_code == 401

    with SessionLocal() as db:
        user = db.query(ClientUser).filter_by(email=email).one()
        assert verify_password("New-Strong-Password-456", user.password_hash)


def test_whatsapp_otp_falls_back_to_text_when_copy_button_is_rejected(tmp_path):
    class RejectInteractiveClient:
        def __init__(self):
            self.fallback_message = None

        def send_interactive_message(self, _destination, _message):
            raise RuntimeError("interactive message rejected")

        def send_message(self, _destination, message):
            self.fallback_message = message

    client = RejectInteractiveClient()
    _send_otp_message(client, "628123456789", "123456", "registration")

    assert client.fallback_message is not None
    assert "123456" in client.fallback_message
    assert "10 menit" in client.fallback_message


def test_whatsapp_connect_captures_qr_using_neonize_qr_callback(tmp_path):
    class FakeClient:
        def qr(self, handler):
            self.qr_handler = handler

    fake_client = FakeClient()
    events = Queue()
    _register_qr_callback(fake_client, events)
    fake_client.qr_handler(fake_client, b"fake-whatsapp-qr-payload")
    kind, payload = events.get_nowait()
    assert kind == "qr"

    manager = WhatsAppOtpManager(tmp_path / "qr-session.db")
    manager._event_queue = Queue()
    manager._event_queue.put((kind, payload))
    status = manager.status()
    assert status["status"] == "waiting_qr"
    assert str(status["qr_data_uri"]).startswith("data:image/svg+xml")


def test_reset_session_deletes_old_files_and_requests_fresh_qr(monkeypatch, tmp_path):
    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.terminated = False

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False

        def join(self, timeout=None):
            return None

        def close(self):
            return None

    session_path = tmp_path / "whatsapp-session.db"
    old_files = [Path(f"{session_path}{suffix}") for suffix in ("", "-wal", "-shm", "-journal")]
    for index, path in enumerate(old_files):
        path.write_bytes(f"old-session-{index}".encode())

    manager = WhatsAppOtpManager(session_path)
    old_process = FakeProcess()
    manager._process = old_process
    manager._state = "waiting_qr"
    new_device_values = []
    monkeypatch.setattr(
        manager,
        "connect",
        lambda *, new_device=False: new_device_values.append(new_device) or {"status": "connecting"},
    )

    result = manager.reset_session()
    assert result["status"] == "connecting"
    assert old_process.terminated is True
    assert new_device_values == [True]
    assert all(not path.exists() for path in old_files)


def test_password_reset_revokes_existing_client_session():
    email = "session-revoke@example.com"
    code = "654321"
    with TestClient(app) as client:
        with SessionLocal() as db:
            user = ClientUser(
                email=email,
                password_hash=hash_password("Old-Test-Password-123"),
                verified_at=utc_now(),
                email_verified_at=utc_now(),
            )
            db.add(user)
            db.commit()
            user_id = user.id
        login = client.post("/api/auth/client-login", json={"email": email, "password": "Old-Test-Password-123"})
        assert login.status_code == 200
        old_cookie = login.headers["set-cookie"].split(";", 1)[0]
        now = utc_now()
        with SessionLocal() as db:
            db.add(PasswordResetCode(
                email_hash=auth_router._password_reset_email_key(email),
                code_hash=auth_router._password_reset_hash(email, code),
                expires_at=now + timedelta(minutes=10),
                resend_after=now,
                attempts=0,
                send_count=1,
                last_activity_at=now,
            ))
            db.commit()
        reset = client.post("/api/auth/password-reset/confirm", json={"email": email, "code": code, "password": "New-Test-Password-456"})
        assert reset.status_code == 200
        assert client.get("/api/mt5/accounts", headers={"Cookie": old_cookie}).status_code == 401
        with SessionLocal() as db:
            session = db.query(AuthSession).filter_by(user_type="client", user_id=str(user_id)).one()
            assert session.revoked_at is not None


def test_public_performance_never_exposes_follower_balance():
    email = "public-privacy-check@example.com"
    with TestClient(app) as client:
        with SessionLocal() as db:
            user = ClientUser(email=email, password_hash=hash_password("A-Strong-Test-Password-789"), verified_at=utc_now())
            db.add(user)
            db.flush()
            account = MT5Account(
                owner_id=user.id,
                label="private-follower",
                server="PrivateBroker",
                login="987654321",
                role="follower",
                token_hash="a" * 64,
            )
            db.add(account)
            db.flush()
            account_id = account.id
            db.add(MT5AccountState(
                account_id=account_id,
                balance=91357.41,
                equity=91357.41,
                floating_profit=0.0,
                margin=0.0,
                free_margin=91357.41,
                currency="USD",
                trade_mode="real",
                allow_live_trading=False,
                terminal_trade_allowed=False,
                expert_trade_allowed=False,
                open_position_ids_json="[]",
                history_cursor="0",
                history_cursor_msc=0,
                observed_at=utc_now(),
            ))
            db.commit()

        response = client.get("/api/public/performance")
        assert response.status_code == 200
        assert "91357.41" not in response.text
        assert "balance_groups" not in response.json()

        with SessionLocal() as db:
            db.query(MT5AccountState).filter_by(account_id=account_id).delete()
            db.query(MT5Account).filter_by(id=account_id).delete()
            db.query(ClientUser).filter_by(email=email).delete()
            db.commit()


def test_settings_endpoint_reads_and_updates_values_after_login():
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "M1-Testing-Password-123"},
        )
        assert login.status_code == 200
        stale_binary = client.get("/downloads/TelegramSignalEA.ex5")
        source_path = PROJECT_ROOT / "mt5" / "TelegramSignalEA.mq5"
        binary_path = PROJECT_ROOT / "mt5" / "TelegramSignalEA.ex5"
        binary_status = (
            410
            if not binary_path.is_file() or source_path.stat().st_mtime > binary_path.stat().st_mtime
            else 200
        )
        assert stale_binary.status_code == binary_status
        current_source = client.get("/downloads/TelegramSignalEA.mq5")
        assert current_source.status_code == 200
        assert "DemoMode" not in current_source.text

        settings = client.get("/api/settings").json()
        assert "demo_mode" not in settings
        invalid_demo_setting = client.put(
            "/api/settings",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"values": {"demo_mode": True}},
        )
        assert invalid_demo_setting.status_code == 422
        update = client.put("/api/settings", headers={"X-CSRF-Token": login.json()["csrf_token"]}, json={"values": {"confidence_threshold": 0.8}})
        assert update.status_code == 200
        assert client.get("/api/settings").json()["confidence_threshold"] == 0.8


def test_settings_endpoint_validates_entry_mode():
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "M1-Testing-Password-123"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        reset = client.put(
            "/api/settings",
            headers={"X-CSRF-Token": csrf},
            json={"values": {"entry_mode": "PARTIAL"}},
        )
        assert reset.status_code == 200

        settings = client.get("/api/settings")
        assert settings.json()["entry_mode"] == "PARTIAL"
        update = client.put(
            "/api/settings",
            headers={"X-CSRF-Token": csrf},
            json={"values": {"entry_mode": "SINGLE"}},
        )
        assert update.status_code == 200
        assert client.get("/api/settings").json()["entry_mode"] == "SINGLE"
        invalid = client.put(
            "/api/settings",
            headers={"X-CSRF-Token": csrf},
            json={"values": {"entry_mode": "MULTI"}},
        )
        assert invalid.status_code == 422
        client.put(
            "/api/settings",
            headers={"X-CSRF-Token": csrf},
            json={"values": {"entry_mode": "PARTIAL"}},
        )


def test_dashboard_mutations_require_csrf_and_validate_risk_settings():
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "M1-Testing-Password-123"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        payload = {"values": {"kill_switch": True, "max_market_deviation_pct": 50, "symbol_mapping": {"GOLD": "XAUUSDm"}}}
        assert client.put("/api/settings", json=payload).status_code == 403
        response = client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json=payload)
        assert response.status_code == 200
        assert response.json()["max_market_deviation_pct"] == 50.0
        invalid = client.put("/api/settings", headers={"X-CSRF-Token": csrf}, json={"values": {"max_market_deviation_pct": 0}})
        assert invalid.status_code == 422
        restored = client.put(
            "/api/settings",
            headers={"X-CSRF-Token": csrf},
            json={"values": {"kill_switch": False, "max_market_deviation_pct": 5, "symbol_mapping": {"GOLD": "XAUUSD"}}},
        )
        assert restored.status_code == 200


def test_request_validation_never_echoes_submitted_api_key():
    key_value = "private-invalid-key"
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "M1-Testing-Password-123"},
        )
        response = client.put(
            "/api/settings/gemini-config",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"api_key": key_value},
        )
        assert response.status_code == 422
        assert key_value not in response.text


def test_csv_export_cells_neutralize_spreadsheet_formulas():
    assert _csv_text("=1+1") == "'=1+1"
    assert _csv_text("  @SUM(A1:A2)") == "'  @SUM(A1:A2)"
    assert _csv_text("ordinary text") == "ordinary text"


def test_startup_syncs_changed_admin_password_from_environment():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    old_config = Settings(
        _env_file=None,
        APP_SECRET_KEY="isolated-test-secret-key-longer-than-32-characters",
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="Previous-test-password-123",
    )
    new_password = "Updated-test-password-456"
    new_config = Settings(
        _env_file=None,
        APP_SECRET_KEY="isolated-test-secret-key-longer-than-32-characters",
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD=new_password,
    )

    with Session(engine) as db:
        seed_admin(db, old_config)
        seed_admin(db, new_config)
        user = db.get(AdminUser, "admin")
        assert user is not None
        assert verify_password(new_password, user.password_hash)
        assert not verify_password("Previous-test-password-123", user.password_hash)

    engine.dispose()


def test_dashboard_endpoints_csv_and_websocket_after_login():
    with TestClient(app) as client:
        assert client.get("/dashboard").status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "M1-Testing-Password-123"},
        )
        assert login.status_code == 200
        assert client.get("/dashboard").status_code == 200

        stats = client.get("/api/stats")
        assert stats.status_code == 200
        assert {"telegram_connected", "gemini_configured", "ea_online", "winrate"} <= stats.json().keys()
        assert client.get("/api/signals?limit=10").status_code == 200
        assert client.get("/api/groups/stats").status_code == 200
        assert client.get("/api/scalper/stats").status_code == 200
        assert client.get("/api/logs?level=ERROR&search=missing").status_code == 200
        assert client.get("/api/trades?result=WIN").status_code == 200
        assert "id,created_at,level,source,message" in client.get("/api/logs/export.csv").text
        assert "source,ticket,group,symbol,action" in client.get("/api/trades/export.csv").text

        with client.websocket_connect("/ws") as websocket:
            event = websocket.receive_json()
            assert event["type"] == "dashboard_refresh"
            assert "stats" in event


def test_signal_endpoint_includes_execution_ticket_and_profit():
    with SessionLocal() as db:
        signal = Signal(
            group_id="dashboard-route-test",
            group_name="Dashboard Test",
            raw_text="BUY XAUUSD",
            status="EXECUTED",
        )
        db.add(signal)
        db.flush()
        db.add(Trade(signal_id=signal.id, ticket="dashboard-route-ticket", profit=12.5))
        db.commit()
        signal_id = signal.id

    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "M1-Testing-Password-123"},
            )
            assert login.status_code == 200
            response = client.get(f"/api/signals?group_id=dashboard-route-test")
            assert response.status_code == 200
            [item] = response.json()
            assert item["id"] == signal_id
            assert item["ticket"] == "dashboard-route-ticket"
            assert item["profit"] == 12.5
    finally:
        with SessionLocal() as db:
            db.query(EAExecution).filter_by(signal_id=signal_id).delete(synchronize_session=False)
            trade = db.query(Trade).filter_by(ticket="dashboard-route-ticket").one_or_none()
            if trade:
                db.delete(trade)
            db.flush()
            signal = db.get(Signal, signal_id)
            if signal:
                db.delete(signal)
            db.commit()
