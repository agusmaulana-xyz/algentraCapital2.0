"""Server-side client for AirPay's QRIS invoice API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import Settings, get_settings


class AirPayError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AirPayInvoice:
    transaction_id: str
    image_tid: str
    merchant_uid: str
    total_amount: int
    unique_code: int
    fee_amount: int | None
    net_amount: int | None
    mdr_rate: str | None
    expiry_at: datetime | None
    status: str


def parse_amount(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise AirPayError(f"Respons AirPay tidak memiliki {field} yang valid.")
    try:
        amount = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AirPayError(f"Respons AirPay tidak memiliki {field} yang valid.") from exc
    if amount < 0 or str(value).strip() not in {str(amount), f"{amount}.0"}:
        raise AirPayError(f"Respons AirPay tidak memiliki {field} yang valid.")
    return amount


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


class AirPayClient:
    """Keep merchant credentials on the backend and use a fixed configured host."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _configuration(self) -> tuple[str, str]:
        settings = self.settings
        if not settings.airpay_configured:
            raise AirPayError("Pembayaran QRIS belum dikonfigurasi. Hubungi administrator.", status_code=503)
        base_url = str(settings.airpay_base_url or "").strip().rstrip("/")
        api_key = settings.airpay_api_key.get_secret_value().strip() if settings.airpay_api_key else ""
        if not base_url or not api_key or api_key.casefold() == "qp-key-your_secret_key":
            raise AirPayError("Kredensial AirPay belum dikonfigurasi. Hubungi administrator.", status_code=503)
        return base_url, api_key

    async def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        base_url, api_key = self._configuration()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{base_url}{path}",
                    json=payload,
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise AirPayError("AirPay terlalu lama merespons. Coba periksa kembali beberapa saat lagi.", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise AirPayError("Server tidak dapat terhubung ke AirPay.", status_code=502) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise AirPayError("AirPay mengirim respons yang tidak dapat dibaca.") from exc
        if not isinstance(body, dict):
            raise AirPayError("Format respons AirPay tidak valid.")
        if not response.is_success:
            if response.status_code in {401, 403}:
                raise AirPayError("AirPay menolak kredensial atau akses merchant.", status_code=502)
            if response.status_code == 409:
                raise AirPayError("Invoice AirPay sudah pernah dibuat.", status_code=409)
            raise AirPayError("AirPay belum dapat memproses permintaan.", status_code=502)
        if body.get("success") is False:
            raise AirPayError("AirPay belum dapat memproses permintaan.")
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise AirPayError("Format data transaksi AirPay tidak valid.")
        return data

    async def create_invoice(self, amount: int, external_id: str) -> AirPayInvoice:
        data = await self._post(
            "/api/qris/create",
            {"amount": amount, "external_id": external_id},
        )
        response_external_id = data.get("external_id")
        if response_external_id is not None and str(response_external_id) != external_id:
            raise AirPayError("AirPay mengembalikan nomor invoice yang berbeda.")

        base_amount = parse_amount(data.get("base_amount", amount), "nominal dasar")
        if base_amount != amount:
            raise AirPayError("Nominal invoice AirPay tidak sama dengan harga pesanan.")
        transaction_id = str(data.get("transaction_id") or "").strip()
        if not transaction_id or len(transaction_id) > 100:
            raise AirPayError("AirPay tidak mengembalikan nomor transaksi yang valid.")
        total_amount = parse_amount(data.get("total_amount"), "total pembayaran")
        if total_amount < base_amount:
            raise AirPayError("Total invoice AirPay tidak valid.")
        unique_code = parse_amount(data.get("unique_code", total_amount - base_amount), "kode unik")
        if total_amount != base_amount + unique_code:
            raise AirPayError("Nominal total invoice AirPay tidak konsisten.")

        image_tid, merchant_uid = self._image_parameters(data, transaction_id)
        return AirPayInvoice(
            transaction_id=transaction_id,
            image_tid=image_tid,
            merchant_uid=merchant_uid,
            total_amount=total_amount,
            unique_code=unique_code,
            fee_amount=self._optional_amount(data.get("fee_amount")),
            net_amount=self._optional_amount(data.get("net_amount")),
            mdr_rate=str(data.get("mdr_rate"))[:32] if data.get("mdr_rate") is not None else None,
            expiry_at=_parse_datetime(data.get("expiry_at")),
            status=self.normalize_status(data.get("status")),
        )

    async def transaction_status(self, transaction_id: str) -> dict[str, Any]:
        return await self._post("/api/qris/status", {"transaction_id": transaction_id})

    async def recover_invoice(self, external_id: str, amount: int) -> AirPayInvoice:
        """Recover a create request whose response was lost after AirPay accepted it."""
        data = await self.transaction_status(external_id)
        response_external_id = data.get("external_id")
        if response_external_id is not None and str(response_external_id) != external_id:
            raise AirPayError("Invoice AirPay yang ditemukan tidak cocok dengan pesanan.")
        base_amount = parse_amount(data.get("base_amount"), "nominal dasar")
        if base_amount != amount:
            raise AirPayError("Nominal invoice AirPay tidak sama dengan harga pesanan.")
        total_amount = parse_amount(data.get("total_amount"), "total pembayaran")
        unique_code = parse_amount(data.get("unique_code", total_amount - base_amount), "kode unik")
        if total_amount != base_amount + unique_code:
            raise AirPayError("Nominal total invoice AirPay tidak konsisten.")
        transaction_id = str(data.get("transaction_id") or external_id).strip()
        image_tid, merchant_uid = self._image_parameters(data, transaction_id)
        return AirPayInvoice(
            transaction_id=transaction_id,
            image_tid=image_tid,
            merchant_uid=merchant_uid,
            total_amount=total_amount,
            unique_code=unique_code,
            fee_amount=self._optional_amount(data.get("fee_amount")),
            net_amount=self._optional_amount(data.get("net_amount")),
            mdr_rate=str(data.get("mdr_rate"))[:32] if data.get("mdr_rate") is not None else None,
            expiry_at=_parse_datetime(data.get("expiry_at")),
            status=self.normalize_status(data.get("status")),
        )

    async def qr_image(self, image_tid: str, merchant_uid: str) -> tuple[bytes, str]:
        base_url, _ = self._configuration()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{base_url}/api/qris/image",
                    params={"tid": image_tid, "mid": merchant_uid},
                )
        except httpx.TimeoutException as exc:
            raise AirPayError("Gambar QR AirPay terlalu lama dimuat.", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise AirPayError("Gambar QR AirPay tidak dapat dimuat.") from exc
        if not response.is_success:
            raise AirPayError("AirPay tidak dapat menyediakan gambar QR untuk invoice ini.")
        media_type = response.headers.get("content-type", "image/png").split(";", 1)[0].casefold()
        signatures = {
            "image/png": response.content.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": response.content.startswith(b"\xff\xd8\xff"),
            "image/webp": len(response.content) >= 12 and response.content[:4] == b"RIFF" and response.content[8:12] == b"WEBP",
        }
        if len(response.content) > 2_000_000 or not signatures.get(media_type, False):
            raise AirPayError("Respons gambar QR AirPay tidak valid.")
        return response.content, media_type

    def _image_parameters(self, data: dict[str, Any], transaction_id: str) -> tuple[str, str]:
        qr_url = data.get("qr_url")
        image_tid = transaction_id
        merchant_uid = ""
        if isinstance(qr_url, str):
            try:
                query = parse_qs(urlsplit(qr_url).query)
                image_tid = (query.get("tid") or [transaction_id])[0].strip()
                merchant_uid = (query.get("mid") or [""])[0].strip()
            except (TypeError, ValueError):
                pass
        configured_uid = (self.settings.airpay_merchant_uid or "").strip()
        if merchant_uid.casefold() == "merchant_uid":
            merchant_uid = ""
        if not merchant_uid and configured_uid.casefold() != "merchant_uid":
            merchant_uid = configured_uid
        if not image_tid or len(image_tid) > 150 or not merchant_uid or len(merchant_uid) > 150:
            raise AirPayError("AirPay tidak mengembalikan identitas merchant untuk gambar QR.")
        return image_tid, merchant_uid

    @staticmethod
    def _optional_amount(value: Any) -> int | None:
        return None if value is None else parse_amount(value, "biaya transaksi")

    @staticmethod
    def normalize_status(value: Any) -> str:
        status = str(value or "PENDING").strip().upper()
        return status[:24] if status else "PENDING"
