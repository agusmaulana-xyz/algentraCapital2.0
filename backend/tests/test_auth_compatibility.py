import hashlib

import bcrypt
from fastapi.testclient import TestClient

from app.auth import hash_client_password, hash_password, verify_password
from app.database import SessionLocal
from app.main import app
from app.models import ClientUser, utc_now
from app.schemas import PasswordResetConfirmRequest, RegisterRequest


def test_client_password_hash_matches_alok_and_verifies_legacy_hashes():
    password = "client-password-8"
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    alok_hash = hash_client_password(password)
    legacy_hash = hash_password(password)

    assert bcrypt.checkpw(digest, alok_hash.encode("ascii"))
    assert verify_password(password, alok_hash)
    assert verify_password(password, legacy_hash)

    long_password = "p" * 80
    assert verify_password(long_password, hash_client_password(long_password))


def test_client_login_accepts_an_alok_hashed_password():
    email = "alok-hash-login@example.com"
    password = "p" * 80
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    password_hash = bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")
    with SessionLocal() as db:
        db.add(ClientUser(email=email, password_hash=password_hash, verified_at=utc_now()))
        db.commit()

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/client-login",
            json={"email": email, "password": password, "remember_me": True},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "authenticated"
    assert "Max-Age=2592000" in response.headers["set-cookie"]


def test_client_registration_and_reset_password_policy_matches_alok():
    assert RegisterRequest(email="client@example.com", password="12345678").password == "12345678"
    long_password = "p" * 80
    assert PasswordResetConfirmRequest(
        email="client@example.com",
        code="123456",
        password=long_password,
    ).password == long_password
