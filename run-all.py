"""Start the web backend, WhatsApp OTP worker, and managed copier together."""

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
COPY_WORKER_SCRIPT = ROOT / "copyTrading" / "scripts" / "run_managed_worker.py"
COPY_WORKER_RUNTIME = ROOT / "copyTrading" / "runtime"
sys.path.insert(0, str(BACKEND_DIR))

from app.whatsapp_worker_protocol import (  # noqa: E402
    WORKER_BASE_URL,
    WORKER_KEY_HEADER,
    worker_api_key,
)


def _worker_request(path: str, method: str = "GET", timeout: float = 1.5) -> bool:
    request = Request(
        f"{WORKER_BASE_URL}{path}",
        headers={WORKER_KEY_HEADER: worker_api_key()},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _stop_worker(process: subprocess.Popen | None, *, owned: bool) -> None:
    if not owned or process is None:
        return
    _worker_request("/shutdown", "POST", timeout=2)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _backend_ready() -> bool:
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=1.5) as response:
            payload = response.read(2048)
            return response.status == 200 and b'"service":"algentra-capital"' in payload
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _stop_copy_worker(
    process: subprocess.Popen | None,
    stop_file: Path | None,
    *,
    owned: bool,
) -> None:
    if stop_file is None:
        return
    if not owned or process is None or process.poll() is not None:
        stop_file.unlink(missing_ok=True)
        return

    try:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.touch(exist_ok=True)
    except OSError as exc:
        print(f"Tidak dapat meminta managed copy worker berhenti ({type(exc).__name__}).", file=sys.stderr)

    try:
        process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        print(
            "Managed copy worker belum berhenti setelah menerima stop; menghentikan proses worker.",
            file=sys.stderr,
        )
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    finally:
        stop_file.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reload", action="store_true", help="Aktifkan auto-reload backend")
    parser.add_argument("--service", action="store_true", help="Mode service tanpa terminal worker terpisah")
    args = parser.parse_args()

    worker_process = None
    owns_worker = False
    copy_worker_process = None
    owns_copy_worker = False
    copy_worker_stop_file = COPY_WORKER_RUNTIME / f"managed-worker-stop-{os.getpid()}-{uuid.uuid4().hex}.signal"
    backend_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--app-dir",
        str(BACKEND_DIR),
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips=127.0.0.1",
    ]
    if args.reload:
        backend_command.extend(["--reload", "--reload-dir", str(BACKEND_DIR)])

    backend_process = None
    try:
        if _worker_request("/health"):
            print("WhatsApp worker yang sudah berjalan akan digunakan.", flush=True)
        else:
            flags = 0
            if os.name == "nt" and not args.service:
                flags = subprocess.CREATE_NEW_CONSOLE
            worker_process = subprocess.Popen(
                [sys.executable, "-m", "app.whatsapp_worker"],
                cwd=BACKEND_DIR,
                creationflags=flags,
            )
            owns_worker = True
            print("Memulai WhatsApp OTP worker di proses terpisah...", flush=True)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if worker_process.poll() is not None:
                    print("WhatsApp worker berhenti saat startup. Periksa jendela terminal worker.", file=sys.stderr)
                    return worker_process.returncode or 1
                if _worker_request("/health"):
                    break
                time.sleep(0.25)
            else:
                print(
                    "WhatsApp worker tidak siap dalam 20 detik. Periksa jendela worker dan pastikan port 8765 bebas.",
                    file=sys.stderr,
                )
                return 1

        print("Starting Algentra Capital at http://127.0.0.1:8000 ...", flush=True)
        backend_process = subprocess.Popen(backend_command, cwd=BACKEND_DIR)
        backend_deadline = time.monotonic() + 90
        while time.monotonic() < backend_deadline:
            if backend_process.poll() is not None:
                print("Backend berhenti sebelum health check berhasil.", file=sys.stderr)
                return backend_process.returncode or 1
            if _backend_ready():
                break
            time.sleep(0.5)
        else:
            print("Backend tidak siap dalam 90 detik; managed copy worker tidak dijalankan.", file=sys.stderr)
            return 1

        copy_worker_stop_file.parent.mkdir(parents=True, exist_ok=True)
        copy_worker_stop_file.unlink(missing_ok=True)
        copy_worker_environment = os.environ.copy()
        copy_worker_environment["ALGENTRA_COPY_WORKER_STOP_FILE"] = str(copy_worker_stop_file)
        copy_worker_flags = 0
        if os.name == "nt" and not args.service:
            copy_worker_flags = subprocess.CREATE_NEW_CONSOLE
        copy_worker_process = subprocess.Popen(
            [sys.executable, str(COPY_WORKER_SCRIPT)],
            cwd=ROOT,
            env=copy_worker_environment,
            creationflags=copy_worker_flags,
        )
        owns_copy_worker = True
        print("Memulai managed copy worker di proses dan terminal terpisah...", flush=True)

        copy_worker_exit_reported = False
        while backend_process.poll() is None:
            if (
                copy_worker_process.poll() is not None
                and not copy_worker_exit_reported
            ):
                print(
                    "Managed copy worker berhenti saat startup. Periksa terminal worker, key copier, config MT5, dan log di copyTrading/runtime/logs.",
                    file=sys.stderr,
                    flush=True,
                )
                copy_worker_exit_reported = True
            time.sleep(0.5)
        return backend_process.returncode
    except KeyboardInterrupt:
        print("Menghentikan backend dan semua worker...", flush=True)
        return 130
    finally:
        _stop_copy_worker(
            copy_worker_process,
            copy_worker_stop_file,
            owned=owns_copy_worker,
        )
        if backend_process is not None and backend_process.poll() is None:
            backend_process.terminate()
            try:
                backend_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend_process.kill()
                backend_process.wait(timeout=5)
        _stop_worker(worker_process, owned=owns_worker)


if __name__ == "__main__":
    raise SystemExit(main())
