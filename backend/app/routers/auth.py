import hashlib
import hmac
import logging
import re
import secrets
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
from ..models import (
    AdminUser,
    AuthSession,
    ClientUser,
    EmailVerification,
    OtpDestinationLimit,
    PasswordResetCode,
    utc_now,
)
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
from ..whatsapp_otp import whatsapp_otp_manager


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
PHONE_SEND_LIMIT = 3


def _set_session_expiry(request: Request, max_age: int, remember_client: bool = False) -> None:
    request.session[AUTH_EXPIRES_SESSION_KEY] = int(utc_now().timestamp()) + max_age
    request.session[REMEMBER_SESSION_KEY] = remember_client


def _normalized_email(value: str) -> str:
    email = value.strip().casefold()
    if not 3 <= len(email) <= 320 or not EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="Format email tidak valid")
    return email


def _normalized_phone(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="Nomor WhatsApp wajib diisi untuk metode OTP WhatsApp")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00") and digits.startswith("00"):
        digits = digits[2:]
    elif not raw.startswith("+") and digits.startswith("0"):
        digits = "62" + digits[1:]
    if not digits.isdigit() or digits.startswith("0") or not 8 <= len(digits) <= 15:
        raise HTTPException(status_code=422, detail="Nomor WhatsApp harus memakai format internasional, contoh 628123456789")
    return digits


def _verification_hash(email: str, code: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, f"{email}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def _password_reset_email_key(email: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, f"password-reset-email:{email}".encode("utf-8"), hashlib.sha256).hexdigest()


def _password_reset_hash(email: str, code: str, channel: str = "email") -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    # Keep existing email challenges valid across deployment; domain-separate WhatsApp codes.
    value = f"password-reset:{email}:{code}" if channel == "email" else f"password-reset:{channel}:{email}:{code}"
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _phone_destination_hash(phone: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, f"otp-destination:whatsapp:{phone}".encode("utf-8"), hashlib.sha256).hexdigest()


def _reserve_phone_destination(db: Session, phone: str, now: datetime) -> tuple[str, int]:
    """Apply a shared per-phone cooldown and hourly cap across OTP purposes."""
    destination_hash = _phone_destination_hash(phone)
    record = db.get(OtpDestinationLimit, destination_hash)
    if record is None:
        record = OtpDestinationLimit(
            destination_hash=destination_hash,
            send_count=0,
            window_started_at=now,
            resend_after=now,
        )
        db.add(record)
        db.flush()

    window_started = _utc(record.window_started_at)
    if window_started + SEND_WINDOW <= now:
        record.window_started_at = now
        record.send_count = 0
        record.locked_until = None
        record.resend_after = now

    locked_for = _seconds_until(record.locked_until, now)
    if locked_for:
        return destination_hash, locked_for
    cooldown = _seconds_until(record.resend_after, now)
    if cooldown:
        return destination_hash, cooldown
    if record.send_count >= PHONE_SEND_LIMIT:
        record.locked_until = window_started + SEND_WINDOW
        return destination_hash, _seconds_until(record.locked_until, now)

    record.send_count += 1
    record.resend_after = now + RESEND_INTERVAL
    if record.send_count >= PHONE_SEND_LIMIT:
        record.locked_until = _utc(record.window_started_at) + SEND_WINDOW
    return destination_hash, 0


def _refund_phone_destination(db: Session, destination_hash: str) -> None:
    record = db.get(OtpDestinationLimit, destination_hash)
    if record is None:
        return
    record.send_count = max(0, record.send_count - 1)
    record.resend_after = utc_now()
    if record.send_count < PHONE_SEND_LIMIT:
        record.locked_until = None
    db.commit()


def _send_otp(channel: str, recipient: str, code: str, purpose: str, settings) -> None:
    if channel == "whatsapp":
        whatsapp_otp_manager.send_otp(recipient, code, purpose)
    else:
        send_verification_code(settings, recipient, code, purpose=purpose)


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


def _deliver_password_reset(settings, channel: str, recipient: str, code: str) -> None:
    try:
        _send_otp(channel, recipient, code, "password_reset", settings)
    except Exception as exc:
        logger.warning("Password reset OTP delivery failed (%s)", type(exc).__name__)


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
    channel = "whatsapp"
    phone = _normalized_phone(payload.phone)
    if not whatsapp_otp_manager.is_connected:
        raise HTTPException(status_code=503, detail="Bot WhatsApp belum tersambung. Hubungi admin untuk mengaktifkan OTP WhatsApp.")

    # Serialize check-and-create so simultaneous submits cannot insert the same email twice.
    db.execute(text("BEGIN IMMEDIATE"))
    if db.execute(select(ClientUser.id).where(ClientUser.email == email)).first():
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    now = utc_now()
    db.query(OtpDestinationLimit).filter(
        OtpDestinationLimit.window_started_at < now - timedelta(days=1)
    ).delete(synchronize_session=False)
    destination_hash: str | None = None
    if phone:
        if db.execute(select(ClientUser.id).where(ClientUser.phone == phone)).first():
            db.rollback()
            return {"status": "verification_sent", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}
        destination_hash, phone_wait = _reserve_phone_destination(db, phone, now)
        if phone_wait:
            db.rollback()
            raise HTTPException(
                status_code=429,
                detail="Pengiriman OTP ke nomor ini dibatasi. Coba lagi nanti.",
                headers={"Retry-After": str(phone_wait)},
            )

    pending = db.get(EmailVerification, email)
    if pending is not None and (pending.channel != "whatsapp" or not pending.phone):
        db.delete(pending)
        db.flush()
        pending = None
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
            channel=channel,
            phone=phone,
            code_hash=code_hash,
            password_hash=password_hash,
            expires_at=now + CODE_LIFETIME,
            resend_after=now,
        )
        db.add(pending)
    else:
        pending.channel = channel
        pending.phone = phone
        pending.code_hash = code_hash
        pending.password_hash = password_hash
    retry_after = _record_code_send(pending, now)
    db.commit()

    try:
        _send_otp(channel, phone or email, code, "registration", settings)
    except Exception as exc:
        # Allow an immediate retry if the selected delivery provider rejected the message.
        current = db.get(EmailVerification, email)
        if current is not None and hmac.compare_digest(current.code_hash, code_hash):
            db.delete(current)
            db.commit()
        else:
            db.rollback()
        if destination_hash:
            _refund_phone_destination(db, destination_hash)
        logger.warning("Registration OTP delivery failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="OTP verifikasi gagal dikirim; coba lagi nanti") from exc
    return {"status": "verification_sent", "retry_after_seconds": retry_after}


def _utc(value):
    return value.replace(tzinfo=utc_now().tzinfo) if value.tzinfo is None else value


@router.post("/resend-code", status_code=status.HTTP_202_ACCEPTED)
def resend_code(payload: ResendCodeRequest, db: Session = Depends(get_db)) -> dict[str, str | int]:
    email = _normalized_email(payload.email)
    settings = get_settings()
    db.execute(text("BEGIN IMMEDIATE"))
    pending = db.get(EmailVerification, email)
    if pending is None or db.execute(select(ClientUser.id).where(ClientUser.email == email)).first():
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}
    channel = "whatsapp"
    phone = pending.phone
    if not phone:
        db.rollback()
        raise HTTPException(status_code=409, detail="Pendaftaran lama tidak memiliki nomor WhatsApp. Mulai daftar kembali dengan nomor Anda.")
    if not whatsapp_otp_manager.is_connected:
        db.rollback()
        raise HTTPException(status_code=503, detail="Bot WhatsApp belum tersambung. Hubungi admin.")

    now = utc_now()
    destination_hash: str | None = None
    destination_hash, phone_wait = _reserve_phone_destination(db, phone, now)
    if phone_wait:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail="Pengiriman OTP ke nomor ini dibatasi. Coba lagi nanti.",
            headers={"Retry-After": str(phone_wait)},
        )
    wait = _send_wait_seconds(pending, now)
    if wait:
        db.rollback()
        return {"status": "verification_sent", "retry_after_seconds": wait}

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _verification_hash(email, code)
    pending.code_hash = code_hash
    pending.channel = channel
    pending.password_hash = hash_client_password(payload.password)
    retry_after = _record_code_send(pending, now)
    db.commit()

    try:
        _send_otp(channel, phone or email, code, "registration", settings)
    except Exception as exc:
        current = db.get(EmailVerification, email)
        if current is not None and hmac.compare_digest(current.code_hash, code_hash):
            db.delete(current)
            db.commit()
        else:
            db.rollback()
        if destination_hash:
            _refund_phone_destination(db, destination_hash)
        logger.warning("Registration resend OTP delivery failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="OTP verifikasi gagal dikirim; coba lagi nanti") from exc
    return {"status": "verification_sent", "retry_after_seconds": retry_after}


@router.post("/verify-otp")
@router.post("/verify-email")
def verify_email(payload: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, str | int]:
    email = _normalized_email(payload.email)
    db.execute(text("BEGIN IMMEDIATE"))
    pending = db.get(EmailVerification, email)
    now = utc_now()
    if pending is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")
    if pending.channel != "whatsapp" or not pending.phone:
        db.delete(pending)
        db.commit()
        raise HTTPException(status_code=400, detail="Pendaftaran lama sudah tidak berlaku. Daftar kembali menggunakan nomor WhatsApp.")
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
        verified_by_email = pending.channel == "email"
        user = ClientUser(
            email=email,
            email_verified_at=now if verified_by_email else None,
            phone=pending.phone if pending.channel == "whatsapp" else None,
            phone_verified_at=now if pending.channel == "whatsapp" else None,
            password_hash=pending.password_hash,
            verified_at=now,
        )
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
    channel = payload.channel
    email_hash = _password_reset_email_key(email)
    db.execute(text("BEGIN IMMEDIATE"))
    now = utc_now()
    db.execute(delete(PasswordResetCode).where(PasswordResetCode.last_activity_at < now - timedelta(hours=2)))
    db.query(OtpDestinationLimit).filter(
        OtpDestinationLimit.window_started_at < now - timedelta(days=1)
    ).delete(synchronize_session=False)
    user = db.execute(select(ClientUser).where(ClientUser.email == email)).scalar_one_or_none()
    if channel == "email" and (not settings.email_configured or user is None):
        db.rollback()
        return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}
    if channel == "whatsapp" and (
        user is None or user.phone is None or user.phone_verified_at is None
    ):
        db.rollback()
        return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    recipient = user.phone if channel == "whatsapp" else email
    if channel == "whatsapp" and recipient:
        _, phone_wait = _reserve_phone_destination(db, recipient, now)
        if phone_wait:
            db.rollback()
            return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    challenge = db.get(PasswordResetCode, email_hash)
    wait = _send_wait_seconds(challenge, now) if challenge else 0
    if wait:
        db.rollback()
        return {"status": "accepted", "retry_after_seconds": int(RESEND_INTERVAL.total_seconds())}

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _password_reset_hash(email, code, channel)
    if challenge is None:
        challenge = PasswordResetCode(
            email_hash=email_hash,
            channel=channel,
            code_hash=code_hash,
            expires_at=now + CODE_LIFETIME,
            resend_after=now,
        )
        db.add(challenge)
    else:
        challenge.channel = channel
        challenge.code_hash = code_hash
    _record_code_send(challenge, now)
    db.commit()

    background_tasks.add_task(_deliver_password_reset, settings, channel, recipient, code)
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
    if challenge.channel != payload.channel:
        db.rollback()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")
    if not hmac.compare_digest(challenge.code_hash, _password_reset_hash(email, payload.code, challenge.channel)):
        challenge.attempts += 1
        challenge.last_activity_at = now
        if challenge.attempts >= CODE_ATTEMPT_LIMIT:
            error = _attempts_exhausted(challenge, now)
            db.commit()
            raise error
        db.commit()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")

    user = db.execute(select(ClientUser).where(ClientUser.email == email)).scalar_one_or_none()
    channel_verified = bool(
        user
        and (
            challenge.channel == "email"
            or (user.phone is not None and user.phone_verified_at is not None)
        )
    )
    if user is None or not channel_verified:
        challenge.attempts += 1
        challenge.last_activity_at = now
        if challenge.attempts >= CODE_ATTEMPT_LIMIT:
            error = _attempts_exhausted(challenge, now)
            db.commit()
            raise error
        db.commit()
        raise HTTPException(status_code=400, detail="Kode tidak valid atau kedaluwarsa")

    user.password_hash = hash_client_password(payload.password)
    if challenge.channel == "email" and user.email_verified_at is None:
        user.email_verified_at = now
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
