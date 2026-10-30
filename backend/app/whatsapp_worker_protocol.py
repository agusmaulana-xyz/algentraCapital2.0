"""Shared local API settings for the isolated WhatsApp OTP worker."""

import hashlib
import hmac

from .config import get_settings


WORKER_HOST = "127.0.0.1"
WORKER_PORT = 8765
WORKER_BASE_URL = f"http://{WORKER_HOST}:{WORKER_PORT}"
WORKER_KEY_HEADER = "X-WhatsApp-Worker-Key"


def worker_api_key() -> str:
    """Derive a local worker credential without adding another secret to .env."""
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, b"algentra:whatsapp-otp-worker:v1", hashlib.sha256).hexdigest()
