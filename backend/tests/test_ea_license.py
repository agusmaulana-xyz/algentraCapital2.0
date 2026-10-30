import hashlib
import math
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import EALicenseKey, SystemLog, utc_now


def test_admin_can_create_and_delete_ternak_uang_license_key():
    key_id = None
    timed_key_id = None
    try:
        with TestClient(app) as client:
            assert client.get("/api/admin/ea-keys").status_code == 401

            login = client.post("/api/auth/login", json={
                "username": "admin",
                "password": "M1-Testing-Password-123",
            })
            assert login.status_code == 200
            csrf_headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            dashboard = client.get("/dashboard")
            assert dashboard.status_code == 200
            assert "API key EA TERNAK UANG" in dashboard.text
            assert "Contoh: 12:00 WIB" in dashboard.text
            assert 'id="ea-key-expires-time" type="text"' in dashboard.text

            no_csrf = client.post("/api/admin/ea-keys", json={"label": "Test key"})
            assert no_csrf.status_code == 403

            past_expiry = client.post(
                "/api/admin/ea-keys",
                headers=csrf_headers,
                json={"label": "Past test", "expires_on": "2000-01-01"},
            )
            assert past_expiry.status_code == 422

            missing_expiry = client.post(
                "/api/admin/ea-keys",
                headers=csrf_headers,
                json={"label": "Missing expiry"},
            )
            assert missing_expiry.status_code == 422

            created = client.post(
                "/api/admin/ea-keys",
                headers=csrf_headers,
                json={"label": "EA TERNAK UANG test", "expires_on": "2099-12-31"},
            )
            assert created.status_code == 201
            result = created.json()
            key_id = result["id"]
            raw_key = result["key"]
            assert raw_key.startswith("ALG-")
            assert result["fingerprint"] == hashlib.sha256(raw_key.encode()).hexdigest()[-8:].upper()
            assert result["expires_at"] == "2099-12-31T23:59:59.999999+07:00"

            timed = client.post(
                "/api/admin/ea-keys",
                headers=csrf_headers,
                json={"label": "EA TERNAK UANG timed test", "expires_on": "2099-12-31", "expires_time": "08:30"},
            )
            assert timed.status_code == 201
            timed_key_id = timed.json()["id"]
            assert timed.json()["expires_at"] == "2099-12-31T08:30:00+07:00"

            listed = client.get("/api/admin/ea-keys")
            assert listed.status_code == 200
            listed_by_id = {item["id"]: item for item in listed.json()}
            assert key_id in listed_by_id
            assert timed_key_id in listed_by_id
            assert listed_by_id[key_id]["expires_at"] == result["expires_at"]
            assert "key" not in listed_by_id[key_id]

            valid = client.post("/api/ea/license/validate", headers={"X-Algentra-API-Key": raw_key})
            assert valid.status_code == 200
            assert valid.json()["valid"] is True
            assert valid.json()["product"] == "EA TERNAK UANG"
            expected_epoch = math.ceil(datetime.fromisoformat(result["expires_at"]).timestamp())
            assert valid.json()["expires_epoch"] == expected_epoch
            updated = {item["id"]: item for item in client.get("/api/admin/ea-keys").json()}
            assert updated[key_id]["last_validated_at"] is not None

            invalid = client.post("/api/ea/license/validate", headers={"X-Algentra-API-Key": "wrong-key"})
            assert invalid.status_code == 401
            assert "wrong-key" not in invalid.text

            deleted = client.delete(f"/api/admin/ea-keys/{key_id}", headers=csrf_headers)
            assert deleted.status_code == 200
            assert deleted.json()["status"] == "deleted"
            assert client.delete(f"/api/admin/ea-keys/{timed_key_id}", headers=csrf_headers).status_code == 200

            revoked = client.post("/api/ea/license/validate", headers={"X-Algentra-API-Key": raw_key})
            assert revoked.status_code == 401
    finally:
        with SessionLocal() as db:
            if key_id is not None:
                db.query(EALicenseKey).filter(EALicenseKey.id == key_id).delete(synchronize_session=False)
            if timed_key_id is not None:
                db.query(EALicenseKey).filter(EALicenseKey.id == timed_key_id).delete(synchronize_session=False)
            db.query(SystemLog).filter(SystemLog.source == "EA_LICENSE").delete(synchronize_session=False)
            db.commit()


def test_expired_ea_license_is_rejected():
    raw_key = "ALG-" + "x" * 40
    with SessionLocal() as db:
        key = EALicenseKey(
            label="expired test",
            key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
            created_at=utc_now() - timedelta(days=2),
            expires_at=utc_now() - timedelta(seconds=1),
        )
        db.add(key)
        db.commit()
        key_id = key.id

    try:
        with TestClient(app) as client:
            response = client.post("/api/ea/license/validate", headers={"X-Algentra-API-Key": raw_key})
            assert response.status_code == 403
            assert response.json()["detail"] == "API key sudah kedaluwarsa"
    finally:
        with SessionLocal() as db:
            db.query(EALicenseKey).filter(EALicenseKey.id == key_id).delete(synchronize_session=False)
            db.query(SystemLog).filter(SystemLog.source == "EA_LICENSE").delete(synchronize_session=False)
            db.commit()
