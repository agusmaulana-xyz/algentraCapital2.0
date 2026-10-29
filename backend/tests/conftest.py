import os
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_SECRET_KEY", "m1-test-secret-key-at-least-thirty-two-characters")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "M1-Testing-Password-123")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("EA_API_KEY", "m4-test-ea-api-key-32-characters-long")


@pytest.fixture(autouse=True)
def disable_external_bot_startup(monkeypatch):
    """Keep app lifespan tests from connecting to credentials in the local .env."""
    from app.main import app, payment_review_bot, telegram_manager, whatsapp_otp_manager
    from app.security import RateLimitMiddleware

    async def no_op():
        return None

    def no_op_sync():
        return None

    monkeypatch.setattr(payment_review_bot, "start", no_op)
    monkeypatch.setattr(payment_review_bot, "shutdown", no_op)
    monkeypatch.setattr(telegram_manager, "startup", no_op)
    monkeypatch.setattr(telegram_manager, "shutdown", no_op)
    monkeypatch.setattr(whatsapp_otp_manager, "startup", no_op_sync)
    monkeypatch.setattr(whatsapp_otp_manager, "shutdown", no_op_sync)

    # Rate limits apply within each test but shouldn't leak between test cases.
    stack = app.middleware_stack or app.build_middleware_stack()
    app.middleware_stack = stack
    while stack is not None:
        if isinstance(stack, RateLimitMiddleware):
            stack.requests.clear()
            break
        stack = getattr(stack, "app", None)
