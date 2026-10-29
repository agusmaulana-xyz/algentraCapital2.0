"""Isolated Neonize session and WhatsApp OTP delivery for the client portal."""

import logging
import multiprocessing
import os
from datetime import datetime, timezone
from multiprocessing.queues import Queue
from pathlib import Path
from queue import Empty
import threading
from time import monotonic
from uuid import uuid4

import segno
from neonize.client import NewClient
from neonize.events import ConnectedEv, ConnectFailureEv, DisconnectedEv
from neonize.ext.interactive_message import ButtonMessage
from neonize.utils.jid import build_jid

from .config import PROJECT_ROOT


logger = logging.getLogger("algentra.whatsapp_otp")
SESSION_PATH = PROJECT_ROOT / "backend" / "data" / "whatsapp_otp_session.db"
PROCESS_STOP_TIMEOUT = 8


def _qr_text(data: bytes | str) -> str:
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def _send_otp_message(client: NewClient, phone: str, code: str, purpose: str) -> None:
    purpose_text = (
        "menyelesaikan pendaftaran akun"
        if purpose == "registration"
        else "mengatur ulang kata sandi"
    )
    body = (
        f"Kode OTP Anda: {code}\n\n"
        f"Gunakan kode ini untuk {purpose_text}. Kode berlaku 10 menit dan hanya dapat digunakan sekali.\n\n"
        "Jangan bagikan kode ini kepada siapa pun. Tim Algentra tidak akan meminta OTP Anda.\n\n"
        "Jika Anda tidak meminta kode ini, abaikan pesan ini."
    )
    destination = build_jid(phone)
    message = (
        ButtonMessage()
        .set_title("Kode keamanan Algentra")
        .set_body(body)
        .set_footer("Jika Anda tidak meminta kode ini, abaikan pesan ini.")
        .add_copy("Salin OTP", code)
    )
    try:
        client.send_interactive_message(destination, message)
    except Exception as exc:
        # Some WhatsApp clients reject this interactive format. Keep delivery working.
        logger.warning(
            "Tombol salin OTP ditolak (%s); mencoba mengirim teks biasa.",
            type(exc).__name__,
        )
        client.send_message(destination, body)


def _publish(event_queue: Queue, kind: str, payload=None) -> None:
    try:
        event_queue.put((kind, payload))
    except (OSError, ValueError):
        pass


def _register_qr_callback(client: NewClient, event_queue: Queue) -> None:
    def on_qr(_: NewClient, data: bytes) -> None:
        qr_data = _qr_text(data)
        if qr_data:
            _publish(event_queue, "qr", qr_data)

    client.qr(on_qr)


def _whatsapp_worker(
    session_path: str,
    new_device: bool,
    command_queue: Queue,
    event_queue: Queue,
    response_queue: Queue,
) -> None:
    """Own Neonize in a child process so a stuck Go FFI call can be terminated safely."""
    try:
        client = NewClient(session_path, new_device=new_device)
    except Exception as exc:
        _publish(event_queue, "error", f"Gagal membuat sesi ({type(exc).__name__}).")
        return

    stop_commands = threading.Event()
    connection_failed = threading.Event()

    _register_qr_callback(client, event_queue)

    @client.event(ConnectedEv)
    def on_connected(_: NewClient, __: ConnectedEv) -> None:
        _publish(event_queue, "state", "connected")

    @client.event(DisconnectedEv)
    def on_disconnected(_: NewClient, __: DisconnectedEv) -> None:
        _publish(event_queue, "state", "disconnected")

    @client.event(ConnectFailureEv)
    def on_connect_failure(_: NewClient, event) -> None:
        connection_failed.set()
        reason = str(event.Reason or event.Message or "Koneksi ditolak WhatsApp")
        _publish(event_queue, "error", reason[:240])

    def run_commands() -> None:
        while not stop_commands.is_set():
            try:
                request_id, action, payload = command_queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                if action == "send_otp":
                    _send_otp_message(
                        client,
                        payload["phone"],
                        payload["code"],
                        payload["purpose"],
                    )
                elif action == "logout":
                    client.logout()
                else:
                    raise ValueError("Perintah worker tidak dikenal")
                response_queue.put((request_id, True, None))
            except Exception as exc:
                response_queue.put((request_id, False, type(exc).__name__))

    command_thread = threading.Thread(
        target=run_commands,
        name="algentra-whatsapp-commands",
        daemon=True,
    )
    command_thread.start()
    _publish(event_queue, "state", "connecting")
    try:
        client.connect()
    except Exception as exc:
        connection_failed.set()
        _publish(event_queue, "error", f"Koneksi gagal ({type(exc).__name__}).")
        logger.exception("Koneksi Neonize WhatsApp OTP gagal.")
    finally:
        stop_commands.set()
        if not connection_failed.is_set():
            _publish(event_queue, "state", "disconnected")


class WhatsAppOtpManager:
    def __init__(self, session_path: Path = SESSION_PATH) -> None:
        self.session_path = session_path
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._rpc_lock = threading.Lock()
        self._process: multiprocessing.Process | None = None
        self._command_queue: Queue | None = None
        self._event_queue: Queue | None = None
        self._response_queue: Queue | None = None
        self._pending_responses: dict[str, tuple[bool, str | None]] = {}
        self._state = "not_started"
        self._qr_payload: str | None = None
        self._error: str | None = None
        self._last_delivery_status: str | None = None
        self._last_delivery_purpose: str | None = None
        self._last_delivery_error: str | None = None
        self._last_delivery_at: str | None = None

    @property
    def is_connected(self) -> bool:
        self._drain_events()
        with self._lock:
            return self._state == "connected" and self._process is not None and self._process.is_alive()

    def startup(self) -> None:
        self.connect()

    def shutdown(self) -> None:
        self.disconnect()

    def connect(self, *, new_device: bool = False) -> dict[str, str]:
        with self._lifecycle_lock:
            self._drain_events()
            with self._lock:
                process = self._process
                if process is not None and process.is_alive():
                    return {"status": self._state}
            if process is not None:
                self._discard_process_handles(process)

            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self._restrict_session_directory()
            context = multiprocessing.get_context("spawn")
            command_queue = context.Queue()
            event_queue = context.Queue()
            response_queue = context.Queue()
            process = context.Process(
                target=_whatsapp_worker,
                args=(
                    str(self.session_path),
                    new_device,
                    command_queue,
                    event_queue,
                    response_queue,
                ),
                name="algentra-whatsapp-otp",
                daemon=True,
            )
            with self._lock:
                self._process = process
                self._command_queue = command_queue
                self._event_queue = event_queue
                self._response_queue = response_queue
                self._pending_responses.clear()
                self._state = "connecting"
                self._qr_payload = None
                self._error = None
            try:
                process.start()
            except Exception as exc:
                self._discard_process_handles(process)
                self._set_state("error", error=f"Gagal menjalankan bot ({type(exc).__name__}).")
                logger.exception("Gagal menjalankan worker WhatsApp OTP.")
            return {"status": self._state}

    def _drain_events(self) -> None:
        with self._lock:
            event_queue = self._event_queue
            process = self._process
        if event_queue is not None:
            while True:
                try:
                    kind, payload = event_queue.get_nowait()
                except Empty:
                    break
                except (OSError, ValueError):
                    break
                if kind == "qr" and payload:
                    with self._lock:
                        self._qr_payload = str(payload)
                        self._state = "waiting_qr"
                        self._error = None
                elif kind == "state":
                    self._set_state(str(payload))
                elif kind == "error":
                    self._set_state("error", error=str(payload or "Koneksi WhatsApp gagal."))
        if process is not None and not process.is_alive():
            with self._lock:
                if self._process is process and self._state not in {"error", "disconnected", "not_started"}:
                    state = "disconnected" if self._state == "connected" else "error"
                    error = None if state == "disconnected" else "Worker WhatsApp berhenti sebelum tersambung."
                    self._state = state
                    self._error = error
                    self._qr_payload = None

    def _set_state(self, state: str, *, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            self._error = error[:240] if error else None
            if state in {"connected", "disconnected", "error"}:
                self._qr_payload = None

    def _request_worker(self, action: str, payload: dict, timeout: float) -> tuple[bool, str | None]:
        with self._rpc_lock:
            with self._lock:
                process = self._process
                command_queue = self._command_queue
                response_queue = self._response_queue
            if process is None or command_queue is None or response_queue is None or not process.is_alive():
                return False, "Worker WhatsApp tidak aktif"

            request_id = uuid4().hex
            try:
                command_queue.put((request_id, action, payload), timeout=2)
            except Exception as exc:
                return False, type(exc).__name__

            deadline = monotonic() + timeout
            while monotonic() < deadline:
                pending = self._pending_responses.pop(request_id, None)
                if pending is not None:
                    return pending
                if not process.is_alive():
                    return False, "Worker WhatsApp berhenti sebelum menjawab"
                try:
                    received_id, succeeded, detail = response_queue.get(timeout=min(0.25, deadline - monotonic()))
                except Empty:
                    continue
                except (OSError, ValueError) as exc:
                    return False, type(exc).__name__
                response = (bool(succeeded), detail)
                if received_id == request_id:
                    return response
                self._pending_responses[received_id] = response
            return False, "Waktu tunggu worker WhatsApp habis"

    def _discard_process_handles(self, process: multiprocessing.Process) -> None:
        with self._lock:
            if self._process is process:
                self._process = None
            queues = (self._command_queue, self._event_queue, self._response_queue)
            self._command_queue = None
            self._event_queue = None
            self._response_queue = None
            self._pending_responses.clear()
        for channel in queues:
            if channel is None:
                continue
            try:
                channel.cancel_join_thread()
                channel.close()
            except (AttributeError, OSError, ValueError):
                pass
        try:
            process.close()
        except (AttributeError, ValueError):
            pass

    def _terminate_worker(self) -> bool:
        with self._lock:
            process = self._process
        if process is None:
            return True
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=PROCESS_STOP_TIMEOUT)
            else:
                process.join(timeout=0)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
            if process.is_alive():
                return False
        except Exception:
            logger.exception("Gagal menghentikan proses worker WhatsApp OTP.")
            return False
        self._discard_process_handles(process)
        return True

    def disconnect(self) -> dict[str, str]:
        with self._lifecycle_lock:
            self._drain_events()
            self._set_state("stopping")
            if not self._terminate_worker():
                self._set_state("error", error="Proses WhatsApp belum berhenti; sesi tetap disimpan.")
                return {"status": "error"}
            self._set_state("disconnected")
            return {"status": "disconnected"}

    def reset_session(self) -> dict[str, str]:
        """Stop the isolated worker, remove its SQLite session, and start fresh pairing."""
        with self._lifecycle_lock:
            self._drain_events()
            was_connected = self.is_connected
            self._set_state("stopping")
            if was_connected:
                succeeded, detail = self._request_worker("logout", {}, timeout=4)
                if not succeeded:
                    logger.warning("Logout WhatsApp dilewati saat reset (%s).", detail or "unknown")
            if not self._terminate_worker():
                self._set_state("error", error="Proses WhatsApp belum berhenti; database sesi tidak dihapus.")
                return {"status": "error"}

            for suffix in ("", "-wal", "-shm", "-journal"):
                path = self.session_path if not suffix else Path(f"{self.session_path}{suffix}")
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    self._set_state(
                        "error",
                        error=f"Database sesi lama tidak bisa dihapus ({type(exc).__name__}).",
                    )
                    logger.exception("Gagal menghapus file sesi WhatsApp OTP.")
                    return {"status": "error"}

            return self.connect(new_device=True)

    def send_otp(self, phone: str, code: str, purpose: str) -> None:
        purpose_label = "registration" if purpose == "registration" else "password_reset"
        if not self.is_connected:
            with self._lock:
                self._last_delivery_status = "failed"
                self._last_delivery_purpose = purpose_label
                self._last_delivery_error = "bot_not_connected"
                self._last_delivery_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            raise RuntimeError("Bot WhatsApp belum tersambung. Admin perlu menautkan perangkat di dashboard.")
        with self._lock:
            self._last_delivery_status = "sending"
            self._last_delivery_purpose = purpose_label
            self._last_delivery_error = None
            self._last_delivery_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        succeeded, detail = self._request_worker(
            "send_otp",
            {"phone": phone, "code": code, "purpose": purpose},
            timeout=45,
        )
        if not succeeded:
            with self._lock:
                self._last_delivery_status = "failed"
                self._last_delivery_error = detail or "unknown_error"
            logger.warning("WhatsApp OTP send failed (purpose=%s, error=%s).", purpose_label, detail or "unknown")
            raise RuntimeError(f"Pengiriman OTP WhatsApp gagal ({detail or 'error'}).")
        with self._lock:
            self._last_delivery_status = "submitted"
            self._last_delivery_error = None

    def status(self) -> dict[str, str | bool | None]:
        self._drain_events()
        with self._lock:
            state = self._state
            qr_payload = self._qr_payload
            error = self._error
            last_delivery_status = self._last_delivery_status
            last_delivery_purpose = self._last_delivery_purpose
            last_delivery_error = self._last_delivery_error
            last_delivery_at = self._last_delivery_at

        qr_data_uri = None
        if state == "waiting_qr" and qr_payload:
            try:
                qr_data_uri = segno.make_qr(qr_payload).svg_data_uri(scale=5, border=4)
            except Exception:
                logger.exception("Gagal membuat gambar QR sesi WhatsApp.")
                error = "QR sesi gagal dibuat; muat ulang halaman admin."

        messages = {
            "not_started": "Sesi WhatsApp belum dimulai.",
            "connecting": "Menghubungkan ke WhatsApp…",
            "waiting_qr": "Pindai QR dari WhatsApp > Perangkat tertaut.",
            "connected": "Nomor WhatsApp sudah tertaut dan siap mengirim OTP.",
            "disconnected": "Sesi WhatsApp terputus. Hubungkan kembali untuk mengirim OTP.",
            "stopping": "Menghentikan proses WhatsApp…",
            "error": error or "Koneksi WhatsApp gagal. Coba hubungkan kembali.",
        }
        return {
            "status": state,
            "connected": state == "connected",
            "message": messages.get(state, "Status WhatsApp tidak diketahui."),
            "qr_data_uri": qr_data_uri,
            "error": error,
            "last_delivery_status": last_delivery_status,
            "last_delivery_purpose": last_delivery_purpose,
            "last_delivery_error": last_delivery_error,
            "last_delivery_at": last_delivery_at,
        }

    def _restrict_session_directory(self) -> None:
        if os.name == "nt":
            return
        try:
            self.session_path.parent.chmod(0o700)
        except OSError:
            logger.warning("Tidak dapat membatasi izin folder sesi WhatsApp.")


whatsapp_otp_manager = WhatsAppOtpManager()
