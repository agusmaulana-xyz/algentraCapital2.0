from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import hash_password
from app.database import SessionLocal
from app.main import app
from app.models import AppSetting, ClientUser, ManagedCopyProfile, MT5Account, PaymentOrder, PendingMT5Verification, utc_now
from app.payment_bot import review_payment_order
from app.routers import mt5 as mt5_router
from app.token_crypto import decrypt_managed_copy_password


def test_managed_copy_client_setup_worker_auth_and_live_acknowledgement(monkeypatch):
    worker_key = "m5-managed-copy-worker-key-at-least-32-characters"
    monkeypatch.setattr(
        mt5_router,
        "get_settings",
        lambda: SimpleNamespace(copier_worker_api_key=SecretStr(worker_key)),
    )
    email = "managed-copy-test@example.com"
    password = "Managed-Client-Password-123"
    account_token_hash = "managed-test-token-hash-unique"
    with TestClient(app) as client:
        with SessionLocal() as db:
            user = ClientUser(
                email=email,
                password_hash=hash_password(password),
                verified_at=utc_now(),
            )
            db.add(user)
            db.flush()
            account = MT5Account(
                owner_id=user.id,
                label="Demo follower",
                server="Broker-Demo",
                login="12345678",
                role="follower",
                execution_mode="MANAGED",
                token_hash=account_token_hash,
            )
            db.add(account)
            db.commit()
            account_id = account.id

        login = client.post("/api/auth/client-login", json={"email": email, "password": password})
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
        worker_headers = {"X-Copier-Worker-Key": worker_key}

        managed_order = {
            "label": "Second managed follower",
            "server": "Broker-Demo",
            "login": "555111",
            "plan": "ZERO",
            "execution_mode": "MANAGED",
        }
        assert client.get("/api/mt5/copier/jobs", headers=worker_headers).status_code == 200
        assert client.post("/api/mt5/copier/heartbeat", headers=worker_headers, json={}).json() == {"status": "ok"}
        assert client.post(
            "/api/mt5/copier/heartbeat", headers={"X-Copier-Worker-Key": "wrong"}, json={}
        ).status_code == 401
        precheck = client.post(
            "/api/mt5/prepayment-checks",
            headers=csrf,
            json={**managed_order, "broker_password": "prepayment-broker-secret"},
        )
        assert precheck.status_code == 202
        check_id = precheck.json()["check_id"]
        pending_checks = client.get("/api/mt5/copier/jobs", headers=worker_headers).json()["prepayment_checks"]
        assert len(pending_checks) == 1
        assert pending_checks[0]["check_id"] == check_id
        assert pending_checks[0]["broker_password"] == "prepayment-broker-secret"
        assert client.post(
            f"/api/mt5/copier/prepayment-checks/{check_id}/result",
            headers=worker_headers,
            json={"valid": True, "message": "Login broker berhasil."},
        ).json()["status"] == "VALID"
        with SessionLocal() as db:
            check = db.get(PendingMT5Verification, check_id)
            assert check is not None
            assert check.password_ciphertext != "prepayment-broker-secret"
            assert decrypt_managed_copy_password(check.password_ciphertext) == "prepayment-broker-secret"

        order_payload = {**managed_order, "prepayment_check_id": check_id}
        with SessionLocal() as db:
            db.delete(db.get(AppSetting, "managed_copy_worker_seen_at"))
            db.commit()
        offline_order = client.post("/api/payments/orders", headers=csrf, json=order_payload)
        assert offline_order.status_code == 409
        assert client.get("/api/mt5/copier/jobs", headers=worker_headers).status_code == 200
        order_response = client.post(
            "/api/payments/orders",
            headers=csrf,
            json=order_payload,
        )
        assert order_response.status_code == 201
        assert order_response.json()["execution_mode"] == "MANAGED"
        order_id = order_response.json()["id"]
        with SessionLocal() as db:
            order = db.get(PaymentOrder, order_id)
            order.status = "PAYMENT_SUBMITTED"
            order.proof_filename = "test-proof.png"
            db.commit()
        approval_status, approved_account_id = review_payment_order(order_id, "accept")
        assert approval_status == "APPROVED"
        assert isinstance(approved_account_id, int)
        with SessionLocal() as db:
            approved_account = db.get(MT5Account, approved_account_id)
            assert approved_account.execution_mode == "MANAGED"
            assert approved_account.token_ciphertext is None
            approved_profile = db.get(ManagedCopyProfile, approved_account_id)
            assert approved_profile is not None
            assert approved_profile.login_status == "VALID"
            assert decrypt_managed_copy_password(approved_profile.password_ciphertext) == "prepayment-broker-secret"
            provisioned_check = db.get(PendingMT5Verification, check_id)
            assert provisioned_check is not None
            assert provisioned_check.status == "PROVISIONED"
            assert provisioned_check.password_ciphertext != "prepayment-broker-secret"

        account_list = client.get("/api/mt5/accounts")
        assert account_list.status_code == 200
        assert account_list.json()[0]["execution_mode"] == "MANAGED"
        approved_summary = next(item for item in account_list.json() if item["id"] == approved_account_id)
        assert approved_summary["managed_copy"]["configured"] is True
        account_page = client.get("/account")
        assert account_page.status_code == 200
        assert "Dikelola server" in account_page.text
        assert "Pasang EA sendiri" in account_page.text

        assert client.get("/api/mt5/copier/jobs").status_code == 401
        setup = client.put(
            f"/api/mt5/accounts/{account_id}/managed-copy",
            headers=csrf,
            json={
                "broker_password": "broker-secret-for-test",
                "follower_symbol": "XAUUSDm",
                "mode_lot": "rasio",
                "ratio_lot": 0.5,
                "lot_tetap": 0.01,
                "max_lot_per_order": 0.05,
                "max_lot_total": 0.1,
                "max_open_positions": 3,
            },
        )
        assert setup.status_code == 200
        assert setup.json()["configured"] is True
        assert "broker_password" not in setup.json()
        assert setup.json()["login_status"] == "CHECKING"

        with SessionLocal() as db:
            profile = db.get(ManagedCopyProfile, account_id)
            assert profile is not None
            assert profile.password_ciphertext != "broker-secret-for-test"
            assert decrypt_managed_copy_password(profile.password_ciphertext) == "broker-secret-for-test"

        invalid_check = client.post(
            f"/api/mt5/copier/jobs/{account_id}/login-check",
            headers=worker_headers,
            json={"valid": False, "message": "Login gagal. Periksa nomor akun, password trading, dan nama server broker."},
        )
        assert invalid_check.status_code == 200
        rejected_start = client.put(
            f"/api/mt5/accounts/{account_id}/managed-copy/active",
            headers=csrf,
            json={"active": True, "acknowledge_live_risk": True},
        )
        assert rejected_start.status_code == 409
        assert "belum lolos pemeriksaan" in rejected_start.json()["detail"]
        setup = client.put(
            f"/api/mt5/accounts/{account_id}/managed-copy",
            headers=csrf,
            json={
                "broker_password": "broker-secret-for-test",
                "follower_symbol": "XAUUSDm",
                "mode_lot": "rasio",
                "ratio_lot": 0.5,
                "lot_tetap": 0.01,
                "max_lot_per_order": 0.05,
                "max_lot_total": 0.1,
                "max_open_positions": 3,
            },
        )
        assert setup.status_code == 200
        assert setup.json()["login_status"] == "CHECKING"

        jobs = client.get("/api/mt5/copier/jobs", headers=worker_headers)
        assert jobs.status_code == 200
        assert jobs.headers["cache-control"] == "no-store"
        assert len(jobs.json()["jobs"]) == 1
        assert jobs.json()["jobs"][0]["account_id"] == approved_account_id
        assert jobs.json()["jobs"][0]["active"] is False
        assert jobs.json()["login_checks"][0]["account_id"] == account_id
        check_result = client.post(
            f"/api/mt5/copier/jobs/{account_id}/login-check",
            headers=worker_headers,
            json={"valid": True, "message": "Login broker berhasil dan terminal siap untuk copy trading."},
        )
        assert check_result.status_code == 200
        assert check_result.json()["status"] == "VALID"
        assert "broker_password" not in client.get("/api/mt5/accounts").text
        assert client.get("/api/mt5/copier/jobs", headers=worker_headers).json()["login_checks"] == []
        assert client.get("/api/mt5/copier/jobs", headers={"X-Copier-Worker-Key": "wrong"}).status_code == 401

        start_path = f"/api/mt5/accounts/{account_id}/managed-copy/active"
        with SessionLocal() as db:
            db.delete(db.get(AppSetting, "managed_copy_worker_seen_at"))
            db.commit()
        offline_start = client.put(
            start_path,
            headers=csrf,
            json={"active": True, "acknowledge_live_risk": True},
        )
        assert offline_start.status_code == 409
        assert client.get("/api/mt5/copier/jobs", headers=worker_headers).status_code == 200
        not_acknowledged = client.put(start_path, headers=csrf, json={"active": True})
        assert not_acknowledged.status_code == 422
        started = client.put(
            start_path,
            headers=csrf,
            json={"active": True, "acknowledge_live_risk": True},
        )
        assert started.status_code == 200
        assert started.json()["active"] is True
        jobs = client.get("/api/mt5/copier/jobs", headers=worker_headers)
        assert jobs.json()["jobs"][0]["broker_password"] == "broker-secret-for-test"
        assert jobs.json()["jobs"][0]["follower_symbol"] == "XAUUSDm"
        assert client.post(
            f"/api/mt5/copier/jobs/{account_id}/status",
            headers=worker_headers,
            json={"status": "RUNNING", "message": None},
        ).status_code == 200
        assert client.get("/api/mt5/accounts").json()[0]["managed_copy"]["login_status"] == "VALID"
        account_report = client.post(
            f"/api/mt5/copier/jobs/{account_id}/report",
            headers=worker_headers,
            json={
                "balance": 1000.0,
                "equity": 1012.5,
                "floating_profit": 12.5,
                "margin": 20.0,
                "free_margin": 992.5,
                "currency": "USD",
                "trade_mode": "demo",
                "allow_live_trading": False,
                "terminal_trade_allowed": True,
                "expert_trade_allowed": True,
                "open_position_ids": [],
                "deals": [
                    {
                        "deal_ticket": "9002",
                        "position_id": "9001",
                        "time_msc": int(utc_now().timestamp() * 1000),
                        "symbol": "XAUUSDm",
                        "action": "BUY",
                        "entry": "IN",
                        "volume": 0.01,
                        "price": 2300.0,
                        "profit": 0.0,
                        "commission": 0.0,
                        "swap": 0.0,
                        "fee": 0.0,
                    },
                    {
                        "deal_ticket": "9003",
                        "position_id": "9001",
                        "time_msc": int(utc_now().timestamp() * 1000) + 1,
                        "symbol": "XAUUSDm",
                        "action": "SELL",
                        "entry": "OUT",
                        "volume": 0.01,
                        "price": 2302.0,
                        "profit": 2.0,
                        "commission": -0.1,
                        "swap": 0.0,
                        "fee": 0.0,
                    },
                ],
            },
        )
        assert account_report.status_code == 200
        assert account_report.json()["history_cursor"] == "9003"
        assert account_report.json()["received_deals"] == 2
        managed_account = client.get("/api/mt5/accounts").json()[0]
        assert managed_account["balance"] == 1000.0
        assert managed_account["equity"] == 1012.5
        history = client.get("/api/mt5/performance").json()
        assert history["performance_groups"][0]["realized_pnl"] == 1.9

        changed_while_active = client.put(
            f"/api/mt5/accounts/{account_id}/managed-copy",
            headers=csrf,
            json={"broker_password": "new-secret"},
        )
        assert changed_while_active.status_code == 409
        paused = client.put(start_path, headers=csrf, json={"active": False})
        assert paused.status_code == 200
        assert paused.json()["status"] == "STANDBY"

        ea_order = {
            "label": "EA follower",
            "server": "Broker-EA-Demo",
            "login": "777222",
            "plan": "ZERO",
            "execution_mode": "EA",
        }
        ea_check = client.post(
            "/api/mt5/prepayment-checks",
            headers=csrf,
            json={**ea_order, "broker_password": "temporary-ea-password"},
        )
        assert ea_check.status_code == 202
        ea_check_id = ea_check.json()["check_id"]
        assert client.post(
            f"/api/mt5/copier/prepayment-checks/{ea_check_id}/result",
            headers=worker_headers,
            json={"valid": True, "message": "Login broker berhasil."},
        ).json()["status"] == "VALID"
        with SessionLocal() as db:
            ea_pending = db.get(PendingMT5Verification, ea_check_id)
            assert ea_pending is not None
            assert ea_pending.password_ciphertext == ""
        ea_order_response = client.post(
            "/api/payments/orders",
            headers=csrf,
            json={**ea_order, "prepayment_check_id": ea_check_id},
        )
        assert ea_order_response.status_code == 201
        ea_order_id = ea_order_response.json()["id"]
        with SessionLocal() as db:
            ea_check = db.get(PendingMT5Verification, ea_check_id)
            assert ea_check is not None
            assert ea_check.status == "CANCELLED"
            assert ea_check.password_ciphertext == ""
            assert ea_check.session_connected is False
            assert db.get(PaymentOrder, ea_order_id).prepayment_check_id == ea_check_id
            order = db.get(PaymentOrder, ea_order_id)
            order.status = "PAYMENT_SUBMITTED"
            db.commit()
        ea_review_status, ea_account_id = review_payment_order(ea_order_id, "accept")
        assert ea_review_status == "APPROVED"
        with SessionLocal() as db:
            ea_account = db.get(MT5Account, ea_account_id)
            assert ea_account is not None
            assert ea_account.token_ciphertext is not None
            assert db.get(ManagedCopyProfile, ea_account_id) is None

        with SessionLocal() as db:
            profile = db.get(ManagedCopyProfile, account_id)
            if profile:
                db.expunge(profile)
            db.query(MT5Account).filter_by(id=account_id).delete()
            db.query(MT5Account).filter_by(id=approved_account_id).delete()
            db.query(PaymentOrder).filter_by(id=order_id).delete()
            db.query(MT5Account).filter_by(id=ea_account_id).delete()
            db.query(PaymentOrder).filter_by(id=ea_order_id).delete()
            db.query(ClientUser).filter_by(email=email).delete()
            db.commit()
