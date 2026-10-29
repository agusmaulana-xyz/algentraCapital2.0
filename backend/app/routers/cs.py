import hashlib
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..cs_service import customer_service
from ..database import get_db
from ..models import CSChatMessage, CSConversation, ClientUser, MT5Account, utc_now
from ..schemas import CSChatMessageRequest
from ..security import csrf_token, require_csrf
from ..token_crypto import TokenEncryptionError, decrypt_account_token


router = APIRouter(prefix="/api/cs", tags=["customer-service"])
logger = logging.getLogger(__name__)
CONVERSATION_LIFETIME = timedelta(hours=24)
PUBLIC_COOKIE = "alg_cs_visitor"
PUBLIC_COOKIE_MAX_AGE = int(CONVERSATION_LIFETIME.total_seconds())
MAX_RETURNED_MESSAGES = 40
TOKEN_MARKER = re.compile(r"\[\[ALGENTRA_TOKEN:(\d+)\]\]")
SECRET_LIKE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def _visitor_serializer() -> URLSafeTimedSerializer:
    secret = get_settings().app_secret_key.get_secret_value()
    return URLSafeTimedSerializer(secret, salt="algentra-public-cs-chat-v1")


def _set_visitor_cookie(response: Response, visitor_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        PUBLIC_COOKIE,
        _visitor_serializer().dumps(visitor_id),
        max_age=PUBLIC_COOKIE_MAX_AGE,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _resolve_identity(request: Request, response: Response, db: Session, *, writing: bool) -> tuple[str, int | None, str | None]:
    client_id = request.session.get("client_user_id")
    if isinstance(client_id, int):
        if db.get(ClientUser, client_id) is None:
            raise HTTPException(status_code=401, detail="Login klien diperlukan")
        if writing:
            require_csrf(request)
        else:
            csrf_token(request)
        return "client", client_id, None
    if request.session.get("admin_username"):
        raise HTTPException(status_code=403, detail="Chat ALGENTRA tersedia untuk pengunjung dan klien")

    visitor_id = None
    raw_cookie = request.cookies.get(PUBLIC_COOKIE)
    if raw_cookie:
        try:
            visitor_id = _visitor_serializer().loads(raw_cookie, max_age=PUBLIC_COOKIE_MAX_AGE)
        except (BadSignature, SignatureExpired):
            visitor_id = None
    if not isinstance(visitor_id, str) or len(visitor_id) < 32:
        visitor_id = secrets.token_urlsafe(32)
    _set_visitor_cookie(response, visitor_id)
    visitor_hash = hashlib.sha256(visitor_id.encode("ascii")).hexdigest()
    return "public", None, visitor_hash


def _check_same_origin(request: Request) -> None:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    try:
        source_host = urlsplit(source).netloc.casefold()
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Origin permintaan tidak valid") from exc
    request_host = request.headers.get("host", "").casefold()
    if not source_host or source_host != request_host:
        raise HTTPException(status_code=403, detail="Permintaan chat harus berasal dari situs ini")


def _expire_old_conversations(db: Session, now) -> None:
    db.execute(delete(CSConversation).where(CSConversation.expires_at <= now))
    db.flush()


def _active_conversation(db: Session, mode: str, client_id: int | None, visitor_hash: str | None, now) -> CSConversation | None:
    query = select(CSConversation).where(CSConversation.expires_at > now)
    if mode == "client":
        query = query.where(CSConversation.client_user_id == client_id)
    else:
        query = query.where(CSConversation.public_visitor_hash == visitor_hash)
    return db.execute(query.order_by(CSConversation.started_at.desc(), CSConversation.id.desc()).limit(1)).scalar_one_or_none()


def _get_or_create_conversation(db: Session, mode: str, client_id: int | None, visitor_hash: str | None) -> CSConversation:
    now = utc_now()
    _expire_old_conversations(db, now)
    conversation = _active_conversation(db, mode, client_id, visitor_hash, now)
    if conversation is None:
        conversation = CSConversation(
            client_user_id=client_id,
            public_visitor_hash=visitor_hash,
            started_at=now,
            expires_at=now + CONVERSATION_LIFETIME,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    else:
        db.commit()
    return conversation


def _owned_accounts(db: Session, client_id: int) -> list[MT5Account]:
    return list(db.execute(
        select(MT5Account).where(MT5Account.owner_id == client_id).order_by(MT5Account.id.asc())
    ).scalars())


def _account_context(accounts: list[MT5Account]) -> list[dict[str, object]]:
    # No login numbers, balances, server credentials, or plaintext tokens go to Gemini.
    return [{
        "label": account.label,
        "active": account.active,
        "plan": account.plan,
        "execution_mode": account.execution_mode,
        "token_available_to_assistant": bool(
            account.active and account.execution_mode != "MANAGED" and account.token_ciphertext
        ),
    } for account in accounts]


def _sanitize_message(message: str) -> str:
    message = message.strip()
    message = SECRET_LIKE.sub("[nilai rahasia disamarkan]", message)
    return TOKEN_MARKER.sub("[token akun disamarkan]", message)[:2000]


def _asks_for_token(message: str) -> bool:
    lowered = message.casefold()
    if "gemini" in lowered and not any(term in lowered for term in ("mt5", "ea", "copy trading", "accounttoken")):
        return False
    explicit_token = any(term in lowered for term in ("token", "api key", "apikey", "accounttoken", "api-key"))
    if explicit_token:
        return True
    if any(term in lowered for term in ("otp", "verifikasi", "kode email", "kode whatsapp", "kode reset", "reset kata sandi")):
        return False
    mentions_code = any(term in lowered for term in ("kode", "kodenya", "kunci"))
    asks_how = any(term in lowered for term in ("masukin", "masukkan", "input", "isi", "pasang", "aktifkan", "mt5", "ea"))
    return mentions_code and asks_how


def _append_account_tokens(reply: str, accounts: list[MT5Account]) -> str:
    if not accounts:
        return reply + "\n\nSaya belum menemukan akun Copy Trading yang terhubung. Tambahkan akun di Portal Klien terlebih dahulu."
    ea_accounts = [account for account in accounts if account.execution_mode != "MANAGED"]
    managed_accounts = [account for account in accounts if account.execution_mode == "MANAGED"]
    if not ea_accounts:
        names = ", ".join(account.label for account in managed_accounts)
        return reply + f"\n\nAkun {names} memakai layanan managed copy dan tidak memerlukan token EA. Atur dan mulai copy trading dari Portal Klien."
    lines = ["\n\nToken EA untuk akun Anda:"]
    for account in accounts:
        if account.execution_mode == "MANAGED":
            lines.append(f"- {account.label}: dikelola server; tidak memakai token EA.")
            continue
        if not account.active:
            lines.append(f"- {account.label}: akun sedang nonaktif. Aktifkan akun dari Portal Klien sebelum memakai EA.")
        elif account.token_ciphertext:
            lines.append(f"- {account.label}: [[ALGENTRA_TOKEN:{account.id}]] — masukkan pada kolom AccountToken di EA Copy Trading.")
        else:
            lines.append(
                f"- {account.label}: token lama belum dapat ditampilkan di chat. Di Portal Klien pilih **Rotasi token**, "
                "salin token baru yang muncul, lalu masukkan pada kolom `AccountToken`. Setelah rotasi, ALGENTRA juga dapat menampilkannya di chat."
            )
    lines.append("Jangan bagikan token ini kepada orang lain.")
    return reply + "\n".join(lines)


def _expand_owned_token_markers(db: Session, text: str, client_id: int | None) -> str:
    if client_id is None:
        return TOKEN_MARKER.sub("[token tersedia di Portal Klien]", text)
    account_cache: dict[int, str] = {}

    def replace(match: re.Match[str]) -> str:
        account_id = int(match.group(1))
        if account_id not in account_cache:
            account = db.execute(select(MT5Account).where(
                MT5Account.id == account_id,
                MT5Account.owner_id == client_id,
                MT5Account.active.is_(True),
            )).scalar_one_or_none()
            if account is None or not account.token_ciphertext:
                account_cache[account_id] = "[token tidak tersedia; gunakan Rotasi token di Portal Klien]"
            else:
                try:
                    account_cache[account_id] = decrypt_account_token(account.token_ciphertext)
                except TokenEncryptionError:
                    account_cache[account_id] = "[token tidak dapat dipulihkan; gunakan Rotasi token di Portal Klien]"
        return account_cache[account_id]

    return TOKEN_MARKER.sub(replace, text)


@router.get("/chat/history")
def get_chat_history(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, object]:
    mode, client_id, visitor_hash = _resolve_identity(request, response, db, writing=False)
    now = utc_now()
    _expire_old_conversations(db, now)
    conversation = _active_conversation(db, mode, client_id, visitor_hash, now)
    messages: list[dict[str, str]] = []
    expires_at = None
    if conversation is not None:
        rows = list(db.execute(
            select(CSChatMessage).where(CSChatMessage.conversation_id == conversation.id)
            .order_by(CSChatMessage.created_at.desc(), CSChatMessage.id.desc())
            .limit(MAX_RETURNED_MESSAGES)
        ).scalars())
        rows.reverse()
        messages = [{
            "role": row.role,
            "content": _expand_owned_token_markers(db, row.content, client_id),
        } for row in rows]
        expires_at = conversation.expires_at.isoformat()
    db.commit()
    return {
        "mode": mode,
        "assistant": "ALGENTRA",
        "messages": messages,
        "expires_at": expires_at,
        "csrf_token": csrf_token(request) if mode == "client" else None,
    }


@router.post("/chat/message")
async def send_chat_message(
    payload: CSChatMessageRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _check_same_origin(request)
    mode, client_id, visitor_hash = _resolve_identity(request, response, db, writing=True)
    message = _sanitize_message(payload.message)
    if not message:
        raise HTTPException(status_code=422, detail="Tulis pesan terlebih dahulu")

    conversation = _get_or_create_conversation(db, mode, client_id, visitor_hash)
    prior_rows = list(db.execute(
        select(CSChatMessage).where(CSChatMessage.conversation_id == conversation.id)
        .order_by(CSChatMessage.created_at.desc(), CSChatMessage.id.desc())
        .limit(MAX_RETURNED_MESSAGES)
    ).scalars())
    prior_rows.reverse()
    history = [{"role": row.role, "content": row.content} for row in prior_rows]
    accounts = _owned_accounts(db, client_id) if client_id is not None else []

    try:
        reply = await customer_service.reply(
            mode=mode,
            message=message,
            history=history,
            account_context=_account_context(accounts) if mode == "client" else None,
        )
    except Exception as exc:
        # Gemini API errors expose a numeric HTTP code (for example 503). Log
        # only that safe classification; exception messages may contain user
        # prompts or other request details.
        upstream_code = getattr(exc, "code", None)
        if isinstance(exc, TimeoutError):
            logger.warning(
                "ALGENTRA CS request failed (TimeoutError, local deadline %.1fs)",
                customer_service.settings.gemini_cs_timeout_seconds,
            )
        elif isinstance(upstream_code, int) and not isinstance(upstream_code, bool):
            logger.warning(
                "ALGENTRA CS request failed (%s, upstream HTTP %d)",
                type(exc).__name__,
                upstream_code,
            )
        else:
            logger.warning("ALGENTRA CS request failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Chat ALGENTRA sedang tidak tersedia. Silakan coba lagi sebentar.",
        ) from exc

    # Model-generated marker text is never trusted. Only markers appended below can disclose a token.
    reply = TOKEN_MARKER.sub("[token akun disamarkan]", reply).strip()
    reply = SECRET_LIKE.sub("[nilai rahasia disamarkan]", reply)[:4000]
    if mode == "client" and _asks_for_token(message):
        reply = _append_account_tokens(reply, accounts)

    now = utc_now()
    db.add_all([
        CSChatMessage(conversation_id=conversation.id, role="user", content=message, created_at=now),
        CSChatMessage(conversation_id=conversation.id, role="assistant", content=reply, created_at=now),
    ])
    db.commit()
    visible_reply = _expand_owned_token_markers(db, reply, client_id)
    return {
        "mode": mode,
        "assistant": "ALGENTRA",
        "message": visible_reply,
        "expires_at": conversation.expires_at.isoformat(),
    }
