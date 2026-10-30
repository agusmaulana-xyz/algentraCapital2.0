"""Dedicated loopback API process that owns the Neonize WhatsApp session."""

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .whatsapp_otp import WhatsAppOtpManager
from .whatsapp_worker_protocol import WORKER_HOST, WORKER_KEY_HEADER, WORKER_PORT, worker_api_key


logger = logging.getLogger("algentra.whatsapp_worker")
MAX_REQUEST_BYTES = 4096


class WhatsAppWorkerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, manager: WhatsAppOtpManager):
        super().__init__((WORKER_HOST, WORKER_PORT), WhatsAppWorkerHandler)
        self.manager = manager
        self.api_key = worker_api_key()


class WhatsAppWorkerHandler(BaseHTTPRequestHandler):
    server: WhatsAppWorkerServer

    def log_message(self, format: str, *args) -> None:
        # Do not log request bodies, phone numbers, or OTP values.
        logger.info("%s - %s", self.address_string(), format % args)

    def _authorized(self) -> bool:
        supplied = self.headers.get(WORKER_KEY_HEADER, "")
        if hmac.compare_digest(supplied, self.server.api_key):
            return True
        self._send_json(401, {"detail": "Worker key tidak valid"})
        return False

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"detail": "Content-Length tidak valid"})
            return None
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._send_json(413, {"detail": "Ukuran request tidak valid"})
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"detail": "JSON tidak valid"})
            return None
        if not isinstance(value, dict):
            self._send_json(400, {"detail": "Body harus berupa object JSON"})
            return None
        return value

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/status":
            self._send_json(200, self.server.manager.status())
        else:
            self._send_json(404, {"detail": "Endpoint tidak ditemukan"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path in {"/connect", "/new-qr", "/disconnect"}:
            try:
                if self.path == "/connect":
                    self.server.manager.connect()
                elif self.path == "/new-qr":
                    self.server.manager.reset_session()
                else:
                    self.server.manager.disconnect()
                self._send_json(200, {"status": "ok"})
            except Exception as exc:
                logger.exception("WhatsApp worker control request failed (%s)", type(exc).__name__)
                self._send_json(503, {"detail": f"Operasi worker gagal ({type(exc).__name__})"})
            return

        if self.path == "/send-otp":
            payload = self._read_json()
            if payload is None:
                return
            phone = payload.get("phone")
            code = payload.get("code")
            purpose = payload.get("purpose")
            if not isinstance(phone, str) or not phone.isdigit() or not 8 <= len(phone) <= 15:
                self._send_json(422, {"detail": "Nomor tujuan tidak valid"})
                return
            if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                self._send_json(422, {"detail": "Kode OTP tidak valid"})
                return
            if purpose not in {"registration", "password_reset"}:
                self._send_json(422, {"detail": "Tujuan OTP tidak valid"})
                return
            try:
                self.server.manager.send_otp(phone, code, purpose)
                self._send_json(200, {"status": "submitted"})
            except Exception as exc:
                logger.warning("OTP delivery failed (%s)", type(exc).__name__)
                self._send_json(503, {"detail": f"Pengiriman OTP gagal ({type(exc).__name__})"})
            return

        if self.path == "/shutdown":
            self._send_json(202, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, name="whatsapp-worker-shutdown", daemon=True).start()
            return
        self._send_json(404, {"detail": "Endpoint tidak ditemukan"})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    manager = WhatsAppOtpManager()
    manager.startup()
    try:
        server = WhatsAppWorkerServer(manager)
    except OSError:
        manager.shutdown()
        logger.exception("Port WhatsApp worker %s sudah digunakan atau tidak dapat dibuka.", WORKER_PORT)
        raise
    logger.info("WhatsApp OTP worker listening on http://%s:%s", WORKER_HOST, WORKER_PORT)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        logger.info("WhatsApp worker dihentikan dari terminal.")
    finally:
        server.server_close()
        manager.shutdown()
        logger.info("WhatsApp OTP worker berhenti.")


if __name__ == "__main__":
    main()
