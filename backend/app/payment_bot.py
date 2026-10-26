"""Telegram bot delivery and admin callbacks for client payment reviews."""

import asyncio
import hashlib
import json
import logging
import secrets
from pathlib import Path

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from .client_plans import CLIENT_PLANS
from .config import PROJECT_ROOT, Settings, get_settings
from .database import SessionLocal
from .models import ManagedCopyProfile, MT5Account, PaymentOrder, PendingMT5Verification, utc_now
from .token_crypto import encrypt_account_token


logger = logging.getLogger("copytrade.payment_bot")
MAX_PROOF_BYTES = 5 * 1024 * 1024
PROOF_MIME_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def payment_proof_path(filename: str) -> Path:
    # filenames are generated server-side; keep uploads outside the static tree.
    return PROJECT_ROOT / "backend" / "data" / "payment_proofs" / Path(filename).name


def _format_rupiah(amount: int) -> str:
    return f"Rp {amount:,}".replace(",", ".")


def _http_error_summary(exc: httpx.HTTPStatusError) -> str:
    """Return Telegram's safe error description without logging request URLs or tokens."""
    try:
        payload = exc.response.json()
    except ValueError:
        payload = {}
    description = payload.get("description") if isinstance(payload, dict) else None
    if not isinstance(description, str) or not description.strip():
        description = "Telegram API returned an HTTP error"
    return f"HTTP {exc.response.status_code}: {description[:240]}"


def review_payment_order(order_id: int, decision: str) -> tuple[str, int | None]:
    """Apply a single admin decision; approval provisions the MT5 account."""
    if decision not in {"accept", "invalid"}:
        raise ValueError("Keputusan pembayaran tidak dikenal")
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        order = db.get(PaymentOrder, order_id)
        if order is None:
            db.rollback()
            return "MISSING", None
        if order.status != "PAYMENT_SUBMITTED":
            db.rollback()
            return order.status, order.account_id

        if decision == "invalid":
            order.status = "REJECTED"
            order.updated_at = utc_now()
            if order.prepayment_check_id:
                check = db.get(PendingMT5Verification, order.prepayment_check_id)
                if check is not None:
                    db.delete(check)
                order.prepayment_check_id = None
            db.commit()
            return order.status, None

        plan = CLIENT_PLANS.get(order.plan)
        if plan is None or order.total_idr != plan["price_idr"]:
            db.rollback()
            return "INVALID_ORDER", None
        duplicate_query = select(MT5Account.id).where(
            func.lower(MT5Account.server) == order.server.casefold(),
            MT5Account.login == order.login,
        )
        if order.execution_mode != "MANAGED":
            duplicate_query = duplicate_query.where(MT5Account.owner_id == order.owner_id)
        duplicate = db.execute(duplicate_query).scalar_one_or_none()
        if duplicate is not None:
            db.rollback()
            return "ACCOUNT_EXISTS", duplicate

        token = secrets.token_urlsafe(32)
        account = MT5Account(
            owner_id=order.owner_id,
            label=order.label,
            server=order.server,
            login=order.login,
            plan=order.plan,
            execution_mode=order.execution_mode,
            role="follower",
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            token_ciphertext=encrypt_account_token(token) if order.execution_mode != "MANAGED" else None,
        )
        db.add(account)
        db.flush()
        if order.execution_mode == "MANAGED" and order.prepayment_check_id:
            check = db.get(PendingMT5Verification, order.prepayment_check_id)
            if (
                check is None
                or check.status != "ORDERED"
                or check.owner_id != order.owner_id
                or check.label != order.label
                or check.server.casefold() != order.server.casefold()
                or check.login != order.login
                or check.plan != order.plan
                or check.execution_mode != order.execution_mode
                or not check.password_ciphertext
            ):
                db.rollback()
                return "LOGIN_CHECK_EXPIRED", None
            db.add(ManagedCopyProfile(
                account_id=account.id,
                password_ciphertext=check.password_ciphertext,
                prepayment_check_id=check.id,
                login_status="VALID",
                login_message="Login MT5 berhasil. Sesi terminal dipertahankan untuk akun ini.",
                active=False,
                status="STANDBY",
                status_message="Copy trading off. Terminal tersambung dan menunggu tombol Mulai.",
            ))
            check.status = "PROVISIONED"
            check.message = "Akun disetujui. Sesi MT5 tetap tersambung dalam mode standby."
        elif order.prepayment_check_id:
            check = db.get(PendingMT5Verification, order.prepayment_check_id)
            if check is not None:
                check.status = "CANCELLED"
                check.message = "Akun EA disetujui. Terminal pemeriksaan sementara dibersihkan."
                check.password_ciphertext = ""
                check.session_connected = False
                check.session_observed_at = None
            order.prepayment_check_id = None
        order.account_id = account.id
        order.status = "APPROVED"
        order.updated_at = utc_now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return "ACCOUNT_EXISTS", None
        return order.status, account.id


class PaymentReviewBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._offset: int | None = None

    @property
    def configured(self) -> bool:
        return self.settings.telegram_bot_token is not None and self.settings.telegram_admin_chat_id is not None

    @property
    def _token(self) -> str:
        if self.settings.telegram_bot_token is None:
            raise RuntimeError("Bot Telegram pembayaran belum dikonfigurasi")
        return self.settings.telegram_bot_token.get_secret_value()

    @property
    def _chat_id(self) -> str:
        return self.settings.telegram_admin_chat_id or ""

    async def start(self) -> None:
        if not self.configured:
            logger.info("Payment review bot disabled: Telegram bot settings are empty")
            return
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
        self._task = asyncio.create_task(self._poll_updates(), name="payment-review-telegram-poll")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, *, data: dict | None = None, files: dict | None = None) -> dict:
        if self._client is None:
            raise RuntimeError("Bot Telegram pembayaran belum aktif")
        response = await self._client.post(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=data,
            files=files,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram menolak permintaan bot")
        return payload.get("result") or {}

    async def notify_order(self, order_id: int) -> bool:
        if not self.configured or self._client is None:
            return False
        with SessionLocal() as db:
            order = db.get(PaymentOrder, order_id)
            if order is None or order.status != "PAYMENT_SUBMITTED" or not order.proof_filename:
                return False
            if order.admin_notified_at is not None:
                return True
            details = {
                "email": order.email,
                "label": order.label,
                "server": order.server,
                "login": order.login,
                "plan": order.plan,
                "amount": order.total_idr,
                "proof": payment_proof_path(order.proof_filename),
            }

        if not details["proof"].is_file():
            logger.warning("Payment order %s proof file is missing", order_id)
            return False
        caption = (
            f"Pembayaran pesanan #{order_id}\n"
            f"Client: {details['email']}\n"
            f"Akun: {details['label']} · {details['login']} · {details['server']}\n"
            f"Paket: {details['plan']}\n"
            f"Total: {_format_rupiah(details['amount'])}\n\n"
            "Periksa bukti transfer, lalu pilih keputusan di bawah."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "Transaksi diterima", "callback_data": f"pay:accept:{order_id}"},
                {"text": "Transaksi tidak valid", "callback_data": f"pay:invalid:{order_id}"},
            ]]
        }
        try:
            with details["proof"].open("rb") as proof_file:
                await self._call(
                    "sendPhoto",
                    data={
                        "chat_id": self._chat_id,
                        "caption": caption,
                        "reply_markup": json.dumps(keyboard),
                    },
                    files={
                        "photo": (
                            details["proof"].name,
                            proof_file,
                            PROOF_MIME_TYPES.get(details["proof"].suffix.casefold(), "application/octet-stream"),
                        )
                    },
                )
            with SessionLocal() as db:
                order = db.get(PaymentOrder, order_id)
                if order is not None and order.status == "PAYMENT_SUBMITTED":
                    order.admin_notified_at = utc_now()
                    db.commit()
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning("Payment Telegram notification failed (%s)", _http_error_summary(exc))
            return False
        except (OSError, httpx.HTTPError, ValueError, RuntimeError) as exc:
            logger.warning("Payment Telegram notification failed (%s)", type(exc).__name__)
            return False

    async def _poll_updates(self) -> None:
        while True:
            try:
                update_params = {"timeout": 25, "allowed_updates": json.dumps(["callback_query"])}
                if self._offset is not None:
                    update_params["offset"] = self._offset
                result = await self._call("getUpdates", data=update_params)
                for update in result if isinstance(result, list) else []:
                    await self._handle_update(update)
                    self._offset = int(update.get("update_id", 0)) + 1
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                logger.warning("Payment Telegram polling failed (%s)", _http_error_summary(exc))
                await asyncio.sleep(5)
            except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
                logger.warning("Payment Telegram polling failed (%s)", type(exc).__name__)
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict) -> None:
        query = update.get("callback_query")
        if not isinstance(query, dict):
            return
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != self._chat_id:
            return
        data = str(query.get("data", ""))
        try:
            prefix, action, raw_order_id = data.split(":", 2)
            if prefix != "pay" or action not in {"accept", "invalid"}:
                raise ValueError
            order_id = int(raw_order_id)
            if order_id < 1:
                raise ValueError
        except ValueError:
            await self._answer_callback(query["id"], "Tombol pesanan tidak valid.", alert=True)
            return

        try:
            status, account_id = await asyncio.to_thread(review_payment_order, order_id, action)
        except Exception as exc:  # keep the bot loop alive if persistence/provisioning fails
            logger.warning("Payment review action failed (%s)", type(exc).__name__)
            await self._answer_callback(query["id"], "Gagal memproses keputusan. Coba lagi.", alert=True)
            return

        if status == "APPROVED":
            answer = f"Transaksi diterima. Akun #{account_id} dibuat."
            caption_status = f"\n\nSTATUS: TRANSAKSI DITERIMA · akun #{account_id} dibuat"
        elif status == "REJECTED":
            answer = "Transaksi ditandai tidak valid."
            caption_status = "\n\nSTATUS: TRANSAKSI TIDAK VALID"
        elif status == "ACCOUNT_EXISTS":
            answer = "Akun dengan server/login ini sudah terdaftar. Perlu pemeriksaan manual."
            caption_status = ""
        elif status == "PAYMENT_SUBMITTED":
            answer = "Bukti masih menunggu keputusan admin."
            caption_status = ""
        else:
            answer = f"Pesanan sudah diproses atau berstatus {status.casefold()}."
            caption_status = ""
        await self._answer_callback(query["id"], answer, alert=status not in {"APPROVED", "REJECTED"})

        if caption_status and message.get("message_id"):
            original_caption = str(message.get("caption", ""))
            try:
                await self._call(
                    "editMessageCaption",
                    data={
                        "chat_id": self._chat_id,
                        "message_id": message["message_id"],
                        "caption": (original_caption + caption_status)[:1024],
                        "reply_markup": json.dumps({"inline_keyboard": []}),
                    },
                )
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                logger.warning("Payment Telegram message update failed (%s)", type(exc).__name__)

    async def _answer_callback(self, callback_id: str, message: str, *, alert: bool) -> None:
        try:
            await self._call(
                "answerCallbackQuery",
                data={"callback_query_id": callback_id, "text": message[:200], "show_alert": str(alert).lower()},
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            logger.warning("Payment Telegram callback response failed (%s)", type(exc).__name__)


payment_review_bot = PaymentReviewBot(get_settings())
