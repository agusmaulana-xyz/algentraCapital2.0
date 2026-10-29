import logging
import secrets
import asyncio
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..airpay import AirPayClient, AirPayError, AirPayInvoice, parse_amount
from ..client_plans import CLIENT_PLANS
from ..config import PROJECT_ROOT, get_settings
from ..database import get_db
from ..models import AppSetting, ClientUser, MT5Account, PaymentOrder, PendingMT5Verification, utc_now
from ..payment_bot import MAX_PROOF_BYTES, payment_proof_path, payment_review_bot, provision_payment_order, review_payment_order
from ..routers.mt5 import _aware, _managed_worker_online, require_client_id
from ..time_utils import wib_iso


logger = logging.getLogger("copytrade.payments")
router = APIRouter(prefix="/api/payments", tags=["payments"])
ACTIVE_ORDER_STATUSES = ("PENDING_PAYMENT", "PAYMENT_SUBMITTED", "CANCEL_REQUESTED")
MAX_ACCOUNTS_PER_USER = 10


class PaymentOrderCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    server: str = Field(min_length=1, max_length=128)
    login: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")
    plan: str = Field(pattern=r"^(ZERO|PRO|EXPERT)$")
    execution_mode: Literal["EA", "MANAGED"] = "EA"
    prepayment_check_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _order_payload(order: PaymentOrder) -> dict[str, object]:
    return {
        "id": order.id,
        "email": order.email,
        "label": order.label,
        "server": order.server,
        "login": order.login,
        "plan": order.plan,
        "execution_mode": order.execution_mode,
        "total_idr": order.total_idr,
        "payment_provider": order.payment_provider,
        "has_qris": bool(order.airpay_transaction_id and order.airpay_merchant_uid),
        "has_airpay_transaction": bool(order.airpay_transaction_id),
        "airpay_status": order.airpay_status,
        "airpay_total_amount": order.airpay_total_amount,
        "airpay_unique_code": order.airpay_unique_code,
        "airpay_fee_amount": order.airpay_fee_amount,
        "airpay_net_amount": order.airpay_net_amount,
        "airpay_mdr_rate": order.airpay_mdr_rate,
        "airpay_expiry_at": wib_iso(order.airpay_expiry_at),
        "airpay_paid_at": wib_iso(order.airpay_paid_at),
        "payment_message": order.payment_message,
        "status": order.status,
        "has_proof": order.proof_filename is not None,
        "account_id": order.account_id,
        "notification_sent": order.admin_notified_at is not None,
        "created_at": wib_iso(order.created_at),
        "updated_at": wib_iso(order.updated_at),
    }


def _proof_kind(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.get("/status")
def payment_status(db: Session = Depends(get_db)) -> dict[str, bool]:
    """Expose only the payment availability flag needed by the client form."""
    item = db.get(AppSetting, "payments_enabled")
    if item is None:
        return {"payments_enabled": True}
    try:
        enabled = json.loads(item.value)
    except (TypeError, json.JSONDecodeError):
        enabled = True
    return {"payments_enabled": enabled is not False}


@router.get("/orders")
def list_payment_orders(owner_id: int = Depends(require_client_id), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    orders = db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.owner_id == owner_id)
        .order_by(PaymentOrder.id.desc())
        .limit(20)
    ).scalars()
    return [_order_payload(order) for order in orders]


@router.get("/orders/{order_id}/proof")
def view_payment_proof(
    order_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> FileResponse:
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None or not order.proof_filename:
        raise HTTPException(status_code=404, detail="Nota atau bukti pembayaran tidak ditemukan")
    proof_path = payment_proof_path(order.proof_filename)
    if not proof_path.is_file():
        raise HTTPException(status_code=404, detail="File bukti pembayaran tidak tersedia")
    media_type = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
        proof_path.suffix.casefold(), "application/octet-stream"
    )
    return FileResponse(
        proof_path,
        media_type=media_type,
        headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"},
    )


@router.post("/orders", status_code=201)
def create_payment_order(
    payload: PaymentOrderCreate,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    label, server, login = payload.label.strip(), payload.server.strip(), payload.login.strip()
    if not label or not server or not login:
        raise HTTPException(status_code=422, detail="Nama akun, server, dan nomor login wajib diisi")
    plan = CLIENT_PLANS[payload.plan]

    # Serialize order creation so simultaneous tabs cannot exceed the account limit.
    db.execute(text("BEGIN IMMEDIATE"))
    payment_setting = db.get(AppSetting, "payments_enabled")
    try:
        payments_enabled = json.loads(payment_setting.value) is not False if payment_setting else True
    except (TypeError, json.JSONDecodeError):
        payments_enabled = True
    if payments_enabled and not get_settings().airpay_configured:
        db.rollback()
        raise HTTPException(status_code=503, detail="Pembayaran AirPay belum dikonfigurasi. Hubungi administrator.")
    check = db.get(PendingMT5Verification, payload.prepayment_check_id)
    if (
        check is None
        or check.owner_id != owner_id
        or check.status != "VALID"
        or _aware(check.expires_at) <= utc_now()
        or (payload.execution_mode == "MANAGED" and not check.password_ciphertext)
        or check.label != label
        or check.server.casefold() != server.casefold()
        or check.login != login
        or check.plan != payload.plan
        or check.execution_mode != payload.execution_mode
    ):
        db.rollback()
        raise HTTPException(status_code=409, detail="Login MT5 belum berhasil diverifikasi untuk data pesanan ini. Periksa kembali koneksi.")
    if payload.execution_mode == "MANAGED" and not _managed_worker_online(db):
        db.rollback()
        raise HTTPException(status_code=409, detail="Layanan managed copy belum tersedia atau worker sedang offline")
    user = db.get(ClientUser, owner_id)
    if user is None:
        db.rollback()
        raise HTTPException(status_code=401, detail="Login pengguna diperlukan")
    account_count = db.execute(
        select(func.count(MT5Account.id)).where(MT5Account.owner_id == owner_id)
    ).scalar_one()
    open_order_count = db.execute(
        select(func.count(PaymentOrder.id)).where(
            PaymentOrder.owner_id == owner_id,
            PaymentOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
    ).scalar_one()
    if account_count + open_order_count >= MAX_ACCOUNTS_PER_USER:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Maksimal {MAX_ACCOUNTS_PER_USER} akun atau pesanan aktif per pengguna")

    duplicate_account = db.execute(
        select(MT5Account.id).where(
            MT5Account.owner_id == owner_id,
            func.lower(MT5Account.server) == server.casefold(),
            MT5Account.login == login,
        )
    ).scalar_one_or_none()
    duplicate_order = db.execute(
        select(PaymentOrder.id).where(
            PaymentOrder.owner_id == owner_id,
            func.lower(PaymentOrder.server) == server.casefold(),
            PaymentOrder.login == login,
            PaymentOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
    ).scalar_one_or_none()
    if payload.execution_mode == "MANAGED":
        duplicate_managed_account = db.execute(
            select(MT5Account.id).where(
                MT5Account.execution_mode == "MANAGED",
                func.lower(MT5Account.server) == server.casefold(),
                MT5Account.login == login,
            )
        ).scalar_one_or_none()
        if duplicate_managed_account is not None:
            db.rollback()
            raise HTTPException(status_code=409, detail="Akun broker ini sudah terdaftar untuk layanan copy server")
        duplicate_managed_order = db.execute(
            select(PaymentOrder.id).where(
                PaymentOrder.execution_mode == "MANAGED",
                func.lower(PaymentOrder.server) == server.casefold(),
                PaymentOrder.login == login,
                PaymentOrder.status.in_(ACTIVE_ORDER_STATUSES),
            )
        ).scalar_one_or_none()
        if duplicate_managed_order is not None:
            db.rollback()
            raise HTTPException(status_code=409, detail="Akun broker ini sudah memiliki pesanan layanan copy server")
    if duplicate_account is not None or duplicate_order is not None:
        db.rollback()
        raise HTTPException(status_code=409, detail="Server dan nomor login ini sudah terdaftar atau memiliki pesanan aktif")

    order = PaymentOrder(
        owner_id=owner_id,
        email=user.email,
        label=label,
        server=server,
        login=login,
        plan=payload.plan,
        execution_mode=payload.execution_mode,
        prepayment_check_id=check.id,
        total_idr=plan["price_idr"],
        payment_provider="AIRPAY" if payments_enabled else "DISABLED",
        airpay_status="NOT_CREATED" if payments_enabled else None,
    )
    db.add(order)
    db.flush()
    if payments_enabled:
        order.airpay_external_id = f"ALG-{order.id}-{secrets.token_hex(6).upper()}"
    if payload.execution_mode == "MANAGED":
        check.status = "ORDERED"
    else:
        # EA mode does not retain the login session, but keep a cleanup tombstone
        # so the worker can remove the temporary terminal folder immediately.
        check.status = "CANCELLED"
        check.message = "Pemeriksaan selesai. Worker akan membersihkan terminal sementara."
        check.password_ciphertext = ""
        check.session_connected = False
        check.session_observed_at = None
    if not payments_enabled:
        status, _account_id = provision_payment_order(db, order)
        if status != "APPROVED":
            db.rollback()
            if status == "LOGIN_CHECK_EXPIRED":
                raise HTTPException(status_code=409, detail="Sesi login MT5 kedaluwarsa. Periksa koneksi akun kembali.")
            if status == "ACCOUNT_EXISTS":
                raise HTTPException(status_code=409, detail="Server dan nomor login ini sudah terdaftar.")
            raise HTTPException(status_code=409, detail="Akun tidak dapat dibuat. Periksa kembali data login MT5.")
        order.payment_message = "Akun dibuat langsung tanpa tagihan karena pembayaran sedang dinonaktifkan."
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Pesanan tidak dapat dibuat. Periksa server dan nomor login.") from exc
    db.refresh(order)
    return _order_payload(order)


def _store_airpay_invoice(order: PaymentOrder, invoice: AirPayInvoice) -> None:
    order.airpay_transaction_id = invoice.transaction_id
    order.airpay_image_tid = invoice.image_tid
    order.airpay_merchant_uid = invoice.merchant_uid
    order.airpay_total_amount = invoice.total_amount
    order.airpay_unique_code = invoice.unique_code
    order.airpay_fee_amount = invoice.fee_amount
    order.airpay_net_amount = invoice.net_amount
    order.airpay_mdr_rate = invoice.mdr_rate
    order.airpay_expiry_at = invoice.expiry_at
    order.airpay_status = invoice.status
    order.payment_message = None
    order.updated_at = utc_now()


def _queue_prepayment_terminal_cleanup(db: Session, order: PaymentOrder, *, retain_credentials: bool = False) -> None:
    if not order.prepayment_check_id:
        return
    check = db.get(PendingMT5Verification, order.prepayment_check_id)
    if check is not None:
        check.status = "CANCEL_REQUESTED" if retain_credentials else "CANCELLED"
        check.message = "Terminal pemeriksaan dihentikan atas permintaan pembatalan."
        check.session_connected = False
        check.session_observed_at = None
        if not retain_credentials:
            check.password_ciphertext = ""
    if not retain_credentials:
        order.prepayment_check_id = None


@router.post("/orders/{order_id}/airpay")
async def create_airpay_invoice(
    order_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Create or safely retry the QRIS invoice for this owner's order."""
    db.execute(text("BEGIN IMMEDIATE"))
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if order.payment_provider != "AIRPAY":
        db.rollback()
        raise HTTPException(status_code=409, detail="Pesanan ini tidak menggunakan pembayaran AirPay")
    if order.status != "PENDING_PAYMENT":
        db.rollback()
        raise HTTPException(status_code=409, detail="Pesanan ini tidak lagi menunggu pembayaran")
    if order.airpay_transaction_id and order.airpay_merchant_uid:
        result = _order_payload(order)
        db.commit()
        return result
    if order.airpay_status == "CREATING" and _aware(order.updated_at) > utc_now() - timedelta(seconds=45):
        db.rollback()
        raise HTTPException(status_code=409, detail="Tagihan QRIS sedang dibuat. Tunggu sebentar lalu muat ulang.")
    if not order.airpay_external_id:
        order.airpay_external_id = f"ALG-{order.id}-{secrets.token_hex(6).upper()}"
    order.airpay_status = "CREATING"
    order.payment_message = None
    order.updated_at = utc_now()
    external_id = order.airpay_external_id
    amount = order.total_idr
    db.commit()

    client = AirPayClient()
    try:
        try:
            invoice = await client.create_invoice(amount, external_id)
        except AirPayError as create_error:
            if create_error.status_code != 409:
                raise
            invoice = await client.recover_invoice(external_id, amount)
    except AirPayError as exc:
        db.execute(text("BEGIN IMMEDIATE"))
        current = db.execute(
            select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
        ).scalar_one_or_none()
        if current is not None and current.status == "PENDING_PAYMENT":
            current.airpay_status = "ERROR"
            current.payment_message = str(exc)[:240]
            current.updated_at = utc_now()
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    db.execute(text("BEGIN IMMEDIATE"))
    current = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if current is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if current.status != "PENDING_PAYMENT":
        db.rollback()
        raise HTTPException(status_code=409, detail="Pesanan berubah saat tagihan QRIS dibuat")
    _store_airpay_invoice(current, invoice)
    db.commit()
    return _order_payload(current)


@router.get("/orders/{order_id}/qris")
async def view_airpay_qr(
    order_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> Response:
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if (
        order is None
        or order.payment_provider != "AIRPAY"
        or order.status != "PENDING_PAYMENT"
        or not order.airpay_transaction_id
        or not order.airpay_image_tid
        or not order.airpay_merchant_uid
    ):
        raise HTTPException(status_code=404, detail="QRIS AirPay tidak tersedia untuk pesanan ini")
    try:
        content, media_type = await AirPayClient().qr_image(order.airpay_image_tid, order.airpay_merchant_uid)
    except AirPayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/orders/{order_id}/airpay/status")
async def check_airpay_order_status(
    order_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if order.payment_provider != "AIRPAY":
        raise HTTPException(status_code=409, detail="Pesanan ini tidak menggunakan pembayaran AirPay")
    if order.status == "APPROVED" or order.status == "EXPIRED" or order.status == "CANCELLED":
        return _order_payload(order)
    if order.status == "PAYMENT_SUBMITTED" and order.airpay_status == "SUCCESS":
        status, _ = await asyncio.to_thread(review_payment_order, order_id, "accept")
        db.expire_all()
        latest = db.get(PaymentOrder, order_id)
        if status != "APPROVED":
            logger.error("Provisioning akun setelah AirPay sukses tertunda untuk pesanan %s (status=%s)", order_id, status)
        return _order_payload(latest) if latest is not None else {"id": order_id, "status": status}
    if order.status not in {"PENDING_PAYMENT", "CANCEL_REQUESTED"} or not order.airpay_transaction_id:
        raise HTTPException(status_code=409, detail="Tagihan QRIS belum tersedia")

    transaction_id = order.airpay_transaction_id
    external_id = order.airpay_external_id
    expected_amounts = (order.total_idr, order.airpay_total_amount, order.airpay_unique_code)
    db.rollback()
    try:
        result = await AirPayClient().transaction_status(transaction_id)
    except AirPayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    response_transaction_id = result.get("transaction_id")
    response_external_id = result.get("external_id")
    if response_transaction_id is not None and str(response_transaction_id) != transaction_id:
        raise HTTPException(status_code=502, detail="Respons status AirPay tidak cocok dengan tagihan ini")
    if response_external_id is not None and str(response_external_id) != external_id:
        raise HTTPException(status_code=502, detail="Referensi status AirPay tidak cocok dengan pesanan")
    airpay_status = AirPayClient.normalize_status(result.get("status"))
    paid_at_value = result.get("paid_at")
    paid_at = None
    if isinstance(paid_at_value, str) and paid_at_value.strip():
        try:
            paid_at = datetime.fromisoformat(paid_at_value.strip().replace("Z", "+00:00"))
        except ValueError:
            paid_at = None

    if airpay_status == "SUCCESS":
        # Status APIs often return only status/paid_at. The exact transaction ID
        # was validated when its invoice was created; validate amounts again if
        # the status response includes them.
        if expected_amounts[1] is None or expected_amounts[2] is None:
            raise HTTPException(status_code=502, detail="Data nominal invoice AirPay tidak lengkap")
        returned_amount_fields = {key for key in ("base_amount", "total_amount", "unique_code") if key in result}
        if returned_amount_fields:
            try:
                base_amount = parse_amount(result.get("base_amount"), "nominal dasar")
                total_amount = parse_amount(result.get("total_amount"), "total pembayaran")
                unique_code = parse_amount(result.get("unique_code", total_amount - base_amount), "kode unik")
            except AirPayError as exc:
                raise HTTPException(status_code=502, detail="AirPay mengembalikan nominal pembayaran yang tidak valid") from exc
            if (
                base_amount != expected_amounts[0]
                or total_amount != expected_amounts[1]
                or unique_code != expected_amounts[2]
                or total_amount != base_amount + unique_code
            ):
                raise HTTPException(status_code=502, detail="Nominal pembayaran AirPay tidak cocok dengan pesanan")

    # Only the gateway can confirm that an invoice expired. A local clock
    # reaching expiry while the gateway still reports Pending must not discard
    # a payment that may already be settling.
    terminal_status = airpay_status in {"EXPIRED", "FAILED", "CANCELLED"}
    db.execute(text("BEGIN IMMEDIATE"))
    current = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if current is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    should_provision = current.status == "PAYMENT_SUBMITTED" and current.airpay_status == "SUCCESS"
    if current.status in {"PENDING_PAYMENT", "CANCEL_REQUESTED"}:
        cancellation_requested = current.status == "CANCEL_REQUESTED"
        current.airpay_status = airpay_status
        current.updated_at = utc_now()
        if airpay_status == "SUCCESS":
            current.status = "PAYMENT_SUBMITTED"
            current.airpay_paid_at = paid_at or utc_now()
            current.payment_message = (
                "Pembayaran masuk sebelum AirPay menyelesaikan pembatalan; akun akan tetap diproses."
                if cancellation_requested else None
            )
            if current.prepayment_check_id:
                check = db.get(PendingMT5Verification, current.prepayment_check_id)
                if check is not None and check.status == "CANCEL_REQUESTED":
                    check.status = "ORDERED"
                    check.message = "Pembayaran diterima; sesi MT5 dipulihkan untuk provisioning akun."
            should_provision = True
        elif terminal_status:
            current.status = "CANCELLED" if cancellation_requested else "EXPIRED"
            current.payment_message = (
                "Tagihan AirPay berakhir dan pesanan berhasil dibatalkan."
                if cancellation_requested else {
                    "EXPIRED": "Tagihan AirPay kedaluwarsa tanpa pembayaran.",
                    "FAILED": "AirPay menyatakan pembayaran gagal. Buat pesanan baru untuk mencoba kembali.",
                    "CANCELLED": "Tagihan AirPay dibatalkan tanpa pembayaran.",
                }[airpay_status]
            )
            _queue_prepayment_terminal_cleanup(db, current)
        elif cancellation_requested:
            current.payment_message = "Permintaan pembatalan menunggu konfirmasi AirPay. Jangan bayar QRIS ini."
        else:
            current.payment_message = None
    db.commit()

    if should_provision:
        provision_status, _ = await asyncio.to_thread(review_payment_order, order_id, "accept")
        db.expire_all()
        current = db.get(PaymentOrder, order_id)
        if provision_status != "APPROVED":
            logger.error(
                "AirPay menerima pembayaran pesanan %s, tetapi provisioning belum selesai (status=%s)",
                order_id,
                provision_status,
            )
            if current is not None and current.status == "PAYMENT_SUBMITTED":
                current.payment_message = "Pembayaran diterima; pembuatan akun sedang menunggu pemeriksaan server."
                db.commit()
                db.refresh(current)
    else:
        db.expire_all()
        current = db.get(PaymentOrder, order_id)
    return _order_payload(current) if current is not None else {"id": order_id, "status": "MISSING"}


@router.post("/orders/{order_id}/cancel")
def cancel_payment_order(
    order_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    db.execute(text("BEGIN IMMEDIATE"))
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if order.status == "CANCEL_REQUESTED":
        result = _order_payload(order)
        db.commit()
        return result
    if order.status not in (*ACTIVE_ORDER_STATUSES, "EXPIRED"):
        db.rollback()
        raise HTTPException(status_code=409, detail="Pesanan ini tidak bisa dibatalkan")
    if order.payment_provider == "AIRPAY":
        if order.status == "PAYMENT_SUBMITTED" or order.airpay_status == "SUCCESS":
            db.rollback()
            raise HTTPException(status_code=409, detail="Pembayaran AirPay sudah berhasil dan sedang diproses; pesanan tidak dapat dibatalkan.")
        if order.airpay_status == "CREATING":
            db.rollback()
            raise HTTPException(status_code=409, detail="Tagihan AirPay sedang dibuat. Tunggu status tagihan diperbarui sebelum membatalkan.")
        if order.airpay_status == "ERROR" and not order.airpay_transaction_id:
            db.rollback()
            raise HTTPException(status_code=409, detail="Status tagihan AirPay belum dapat dipastikan. Coba buat ulang atau periksa status tagihan sebelum membatalkan.")
        if order.airpay_transaction_id and order.airpay_status not in {"EXPIRED", "FAILED", "CANCELLED"}:
            order.status = "CANCEL_REQUESTED"
            order.payment_message = "Permintaan pembatalan menunggu konfirmasi AirPay. Jangan bayar QRIS ini."
            order.updated_at = utc_now()
            _queue_prepayment_terminal_cleanup(db, order, retain_credentials=True)
            db.commit()
            return _order_payload(order)
        if order.airpay_status == "PENDING":
            db.rollback()
            raise HTTPException(status_code=409, detail="Tagihan AirPay belum memiliki ID transaksi yang valid.")
    old_proof = payment_proof_path(order.proof_filename) if order.proof_filename else None
    order.status = "CANCELLED"
    order.updated_at = utc_now()
    _queue_prepayment_terminal_cleanup(db, order)
    if order.payment_provider == "AIRPAY":
        order.payment_message = "Pesanan dibatalkan. Sesi terminal pemeriksaan sedang dibersihkan."
    db.commit()
    if old_proof is not None:
        try:
            old_proof.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove cancelled payment proof (%s)", type(exc).__name__)
    return _order_payload(order)


@router.post("/orders/{order_id}/proof")
async def submit_payment_proof(
    order_id: int,
    file: UploadFile = File(...),
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if order.payment_provider == "AIRPAY":
        raise HTTPException(status_code=409, detail="Pembayaran AirPay diverifikasi otomatis; bukti manual tidak diperlukan.")
    if order.status != "PENDING_PAYMENT":
        raise HTTPException(status_code=409, detail="Bukti hanya dapat dikirim untuk pesanan yang menunggu pembayaran")

    content = await file.read(MAX_PROOF_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=422, detail="File bukti transaksi kosong")
    if len(content) > MAX_PROOF_BYTES:
        raise HTTPException(status_code=413, detail="Ukuran bukti transaksi maksimal 5 MB")
    detected_type = _proof_kind(content)
    if detected_type is None or file.content_type != detected_type:
        raise HTTPException(status_code=415, detail="Gunakan gambar bukti transaksi JPG, PNG, atau WebP")

    # Re-check under a write lock so a concurrent cancel or duplicate upload cannot win.
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if order.status != "PENDING_PAYMENT":
        db.rollback()
        raise HTTPException(status_code=409, detail="Bukti hanya dapat dikirim untuk pesanan yang menunggu pembayaran")

    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[detected_type]
    filename = f"{order.id}-{secrets.token_urlsafe(18)}.{extension}"
    destination = payment_proof_path(filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    order.proof_filename = filename
    order.status = "PAYMENT_SUBMITTED"
    order.updated_at = utc_now()
    try:
        db.commit()
    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise
    db.refresh(order)
    notification_sent = await payment_review_bot.notify_order(order.id)
    payload = _order_payload(order)
    payload["notification_sent"] = notification_sent
    return payload


@router.post("/orders/{order_id}/notify")
async def retry_admin_notification(
    order_id: int,
    owner_id: int = Depends(require_client_id),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    order = db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id, PaymentOrder.owner_id == owner_id)
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    if order.status != "PAYMENT_SUBMITTED":
        raise HTTPException(status_code=409, detail="Pesanan tidak sedang menunggu pemeriksaan pembayaran")
    if order.payment_provider == "AIRPAY":
        raise HTTPException(status_code=409, detail="Pembayaran AirPay diverifikasi otomatis")
    if order.admin_notified_at is not None:
        return {"notification_sent": True}
    return {"notification_sent": await payment_review_bot.notify_order(order_id)}


@router.get("/qris.jpeg", include_in_schema=False)
def qris_image() -> FileResponse:
    return FileResponse(
        PROJECT_ROOT / "backend" / "app" / "static" / "qris.jpeg",
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )
