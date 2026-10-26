import hashlib
import hmac
import logging
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from math import ceil

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import hash_client_password, hash_password, verify_password
from ..config import get_settings
from ..database import get_db
from ..email_service import send_verification_code
from ..models import AdminUser, AuthSession, ClientUser, EmailVerification, PasswordResetCode, utc_now
from ..registration import registration_is_open
from ..security import (
    AUTH_EXPIRES_SESSION_KEY,
    REMEMBER_CLIENT_MAX_AGE,
    REMEMBER_SESSION_KEY,
    SESSION_DEFAULT_MAX_AGE,
    csrf_token,
    require_csrf,
)
from ..schemas import (
    ClientLoginRequest,
    LoginRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
    ResendCodeRequest,
    VerifyEmailRequest,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
logger = logging.getLogger(__name__)
# Perform the same bcrypt work for unknown usernames and email addresses.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
RESEND_INTERVAL = timedelta(minutes=1)
SEND_WINDOW = timedelta(hours=1)
SEND_LIMIT = 3
CODE_LIFETIME = timedelta(minutes=10)
CODE_ATTEMPT_LIMIT = 3


def _set_session_expiry(request: Request, max_age: int, remember_client: bool = False) -> None:
    request.session[AUTH_EXPIRES_SESSION_KEY] = int(utc_now().timestamp()) + max_age
    request.session[REMEMBER_SESSION_KEY] = remember_client


def _normalized_email(value: str) -> str:
    email = value.strip().casefold()
    if not 3 <= len(email) <= 320 or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="Format email tidak valid")
    return email


def _verification_hash(email: str, code: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, f"{email}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def _password_reset_email_key(email: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, f"password-reset-email:{email}".encode("utf-8"), hashlib.sha256).hexdigest()


def _password_reset_hash(email: str, code: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, f"password-reset:{email}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def _seconds_until(deadline: datetime | None, now: datetime) -> int:
    if deadline is None:
        return 0
    remaining = (_utc(deadline) - now).total_seconds()
    return max(0, ceil(remaining))


def _send_wait_seconds(record: EmailVerification | PasswordResetCode, now: datetime) -> int:
    locked = _seconds_until(record.locked_until, now)
    if locked:
        return locked
    cooldown = _seconds_until(record.resend_after, now)
    if cooldown:
        return cooldown
    window_started = record.send_window_started_at
    if record.send_count >= SEND_LIMIT and window_started is not None:
        return _seconds_until(_utc(window_started) + SEND_WINDOW, now)
    return 0


def _record_code_send(record: EmailVerification | PasswordResetCode, now: datetime) -> int:
    record.send_count = record.send_count or 0
    window_started = record.send_window_started_at
    if window_started is None or _utc(window_started) + SEND_WINDOW <= now:
        record.send_window_started_at = now
        record.send_count = 0
        record.locked_until = None

    record.send_count += 1
    record.resend_after = now + RESEND_INTERVAL
    record.expires_at = now + CODE_LIFETIME
    record.attempts = 0
    if isinstance(record, PasswordResetCode):
        record.last_activity_at = now
    if record.send_count >= SEND_LIMIT:
        record.locked_until = now + SEND_WINDOW
    return _send_wait_seconds(record, now)


def _attempts_exhausted(record: EmailVerification | PasswordResetCode, now: datetime) -> HTTPException:
    record.locked_until = now + SEND_WINDOW
    record.resend_after = record.locked_until
    return HTTPException(
        status_code=429,
        detail="Kesempatan memasukkan kode habis. Coba lagi dalam 1 jam.",
        headers={"Retry-After": str(int(SEND_WINDOW.total_seconds()))},
    )


def _deliver_password_reset(settings, email: str, code: str) -> None:
    try:
        send_verification_code(settings, email, code, purpose="password_reset")
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("Password reset email delivery failed (%s)", type(exc).__name__)


@router.post("/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.get(AdminUser, payload.username)
    password_valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not password_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Username atau password salah")
    request.session.clear()
    request.session["admin_username"] = user.username
    _set_session_expiry(request, SESSION_DEFAULT_MAX_AGE)
    token = csrf_token(request)
    return {"status": "authenticated", "username": user.username, "csrf_token": token}


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> dict[str, str | int]:
    if not registration_is_open(db):
        raise HTTPException(status_code=403, detail="Pendaftaran client sedang ditutup. Silakan tunggu pengumuman launching.")
    email = _normalized_email(payload.email)
    settings = get_settings()
    if not settings.email_configured:
        raise HTTPException(status_code=503, detail="Pendaftaran belum tersedia karena email SMTP belum dikonfigurasi")
    # Serialize check-and-create so simultaneous submits cannot insert the same email twice.
    db.execute(text("BEGIN IMMEDIATE"))
    if db.execute(select(ClientUser.id).where(ClientUser.email == email)).first():
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    now = utc_now()
    pending = db.get(EmailVerification, email)
    wait = _send_wait_seconds(pending, now) if pending else 0
    if wait:
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": wait}

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _verification_hash(email, code)
    password_hash = hash_client_password(payload.password)
    if pending is None:
        pending = EmailVerification(
            email=email,
            code_hash=code_hash,
            password_hash=password_hash,
            expires_at=now + CODE_LIFETIME,
            resend_after=now,
        )
        db.add(pending)
    else:
        pending.code_hash = code_hash
        pending.password_hash = password_hash
    retry_after = _record_code_send(pending, now)
    db.commit()

    try:
        send_verification_code(settings, email, code)
    except (OSError, smtplib.SMTPException) as exc:
        # Allow an immediate retry if the email provider rejected the message.
        current = db.get(EmailVerification, email)
        if current is not None and hmac.compare_digest(current.code_hash, code_hash):
            db.delete(current)
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=503, detail="Email verifikasi gagal dikirim; coba lagi nanti") from exc
    return {"status": "verification_sent", "retry_after_seconds": retry_after}


def _utc(value):
    return value.replace(tzinfo=utc_now().tzinfo) if value.tzinfo is None else value


@router.post("/resend-code", status_code=status.HTTP_202_ACCEPTED)
def resend_code(payload: ResendCodeRequest, db: Session = Depends(get_db)) -> dict[str, str | int]:
    email = _normalized_email(payload.email)
    settings = get_settings()
    if not settings.email_configured:
        raise HTTPException(status_code=503, detail="Pendaftaran belum tersedia karena email SMTP belum dikonfigurasi")

    db.execute(text("BEGIN IMMEDIATE"))
    pending = db.get(EmailVerification, email)
    if pending is None or db.execute(select(ClientUser.id).where(ClientUser.email == email)).first():
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}
    now = utc_now()
    wait = _send_wait_seconds(pending, now)
    if wait:
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": wait}

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _verification_hash(email, code)
    pending.code_hash = code_hash
    pending.password_hash = hash_client_password(payload.password)
    retry_after = _record_code_send(pending, now)
    db.commit()

    try:
        send_verification_code(settings, email, code)
    except (OSError, smtplib.SMTPException) as exc:
        current = db.get(EmailVerification, email)
        if current is not None and hmac.compare_digest(current.code_hash, code_hash):
            db.delete(current)
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=503, detail="Email verifikasi gagal dikirim; coba lagi nanti") from exc
    return {"status": "verification_sent", "retry_after_seconds": retry_after}


@router.post("/verify-email")
def verify_email(payload: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, str | int]:
    email = _normalized_email(payload.email)
    db.execute(text("BEGIN IMMEDIATE"))
    pending = db.get(EmailVerification, email)
    now = utc_now()
    if pending is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")
    locked_for = _seconds_until(pending.locked_until, now)
    if locked_for:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail="Kesempatan memasukkan kode habis. Coba lagi dalam 1 jam.",
            headers={"Retry-After": str(locked_for)},
        )
    if _utc(pending.expires_at) <= now:
        db.rollback()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")
    if pending.attempts >= CODE_ATTEMPT_LIMIT:
        error = _attempts_exhausted(pending, now)
        db.commit()
        raise error
    if not hmac.compare_digest(pending.code_hash, _verification_hash(email, payload.code)):
        pending.attempts += 1
        if pending.attempts >= CODE_ATTEMPT_LIMIT:
            error = _attempts_exhausted(pending, now)
            db.commit()
            raise error
        db.commit()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")

    user = db.execute(select(ClientUser).where(ClientUser.email == email)).scalar_one_or_none()
    if user is None:
        user = ClientUser(email=email, password_hash=pending.password_hash, verified_at=now)
        db.add(user)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Akun dengan email tersebut sudah dibuat") from exc
    db.delete(pending)
    db.commit()
    request.session.clear()
    request.session["client_user_id"] = user.id
    _set_session_expiry(request, SESSION_DEFAULT_MAX_AGE)
    return {"status": "verified", "user_id": user.id, "csrf_token": csrf_token(request)}


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
def request_password_reset(payload: PasswordResetRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict[str, str | int]:
    email = _normalized_email(payload.email)
    settings = get_settings()
    email_hash = _password_reset_email_key(email)
    db.execute(text("BEGIN IMMEDIATE"))
    now = utc_now()
    db.execute(delete(PasswordResetCode).where(PasswordResetCode.last_activity_at < now - timedelta(hours=2)))
    user = db.execute(select(ClientUser).where(ClientUser.email == email)).scalar_one_or_none()
    if not settings.email_configured:
        db.rollback()
        return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}
    if user is None:
        db.rollback()
        return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    challenge = db.get(PasswordResetCode, email_hash)
    wait = _send_wait_seconds(challenge, now) if challenge else 0
    if wait:
        db.rollback()
        return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _password_reset_hash(email, code)
    if challenge is None:
        challenge = PasswordResetCode(
            email_hash=email_hash,
            code_hash=code_hash,
            expires_at=now + CODE_LIFETIME,
            resend_after=now,
        )
        db.add(challenge)
    else:
        challenge.code_hash = code_hash
    _record_code_send(challenge, now)
    db.commit()

    background_tasks.add_task(_deliver_password_reset, settings, email, code)
    return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}


@router.post("/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirmRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = _normalized_email(payload.email)
    db.execute(text("BEGIN IMMEDIATE"))
    challenge = db.get(PasswordResetCode, _password_reset_email_key(email))
    now = utc_now()
    if challenge is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")
    locked_for = _seconds_until(challenge.locked_until, now)
    if locked_for:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail="Kesempatan memasukkan kode habis. Coba lagi dalam 1 jam.",
            headers={"Retry-After": str(locked_for)},
        )
    if _utc(challenge.expires_at) <= now:
        db.rollback()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")
    if not hmac.compare_digest(challenge.code_hash, _password_reset_hash(email, payload.code)):
        challenge.attempts += 1
        challenge.last_activity_at = now
        if challenge.attempts >= CODE_ATTEMPT_LIMIT:
            error = _attempts_exhausted(challenge, now)
            db.commit()
            raise error
        db.commit()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")

    user = db.execute(select(ClientUser).where(ClientUser.email == email)).scalar_one_or_none()
    if user is None:
        challenge.attempts += 1
        challenge.last_activity_at = now
        if challenge.attempts >= CODE_ATTEMPT_LIMIT:
            error = _attempts_exhausted(challenge, now)
            db.commit()
            raise error
        db.commit()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")

    user.password_hash = hash_client_password(payload.password)
    db.query(AuthSession).filter(
        AuthSession.user_type == "client",
        AuthSession.user_id == str(user.id),
        AuthSession.revoked_at.is_(None),
    ).update({AuthSession.revoked_at: now}, synchronize_session=False)
    db.delete(challenge)
    db.commit()
    return {"status": "password_reset"}


@router.post("/client-login")
def client_login(payload: ClientLoginRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, str | int]:
    email = _normalized_email(payload.email)
    user = db.execute(select(ClientUser).where(ClientUser.email == email)).scalar_one_or_none()
    password_valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not password_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email atau kata sandi salah")
    request.session.clear()
    request.session["client_user_id"] = user.id
    max_age = REMEMBER_CLIENT_MAX_AGE if payload.remember_me else SESSION_DEFAULT_MAX_AGE
    _set_session_expiry(request, max_age, remember_client=payload.remember_me)
    return {"status": "authenticated", "user_id": user.id, "csrf_token": csrf_token(request)}


@router.post("/logout")
def logout(request: Request) -> dict[str, str]:
    require_csrf(request)
    request.session.clear()
    return {"status": "logged_out"}
