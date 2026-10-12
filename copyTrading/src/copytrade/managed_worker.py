"""Run server-managed follower jobs fetched from the Algentra client portal."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .logging_setup import configure_logging

logger = logging.getLogger("managed_worker")


def enable_terminal_auto_trading(terminal_dir: str | Path) -> Path:
    """Enable MT5 terminal-level algo trading in a portable terminal copy."""
    config_path = Path(terminal_dir) / "Config" / "common.ini"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    raw = config_path.read_bytes() if config_path.exists() else b""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
        text = raw.decode(encoding)
    else:
        encoding = "utf-8-sig"
        text = raw.decode(encoding) if raw else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    output: list[str] = []
    experts_seen = False
    keys = {
        "allowlivetrading": "AllowLiveTrading=1",
        "enabled": "Enabled=1",
        "account": "Account=0",
        "profile": "Profile=0",
    }
    present: set[str] = set()
    in_experts = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_experts:
                output.extend(value for key, value in keys.items() if key not in present)
            in_experts = stripped[1:-1].casefold() == "experts"
            experts_seen = experts_seen or in_experts
            present = set()
            output.append(line)
            continue
        if in_experts and "=" in stripped and not stripped.startswith((";", "#")):
            key = stripped.split("=", 1)[0].strip().casefold()
            if key in keys:
                output.append(keys[key])
                present.add(key)
                continue
        output.append(line)
    if in_experts:
        output.extend(value for key, value in keys.items() if key not in present)
    if not experts_seen:
        if output and output[-1].strip():
            output.append("")
        output.extend(("[Experts]", *keys.values()))
    rendered = newline.join(output) + newline
    if rendered == text:
        return config_path
    temporary = config_path.with_name(f"{config_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_bytes(rendered.encode(encoding))
        temporary.replace(config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return config_path


def acquire_worker_lock(runtime_dir: str | Path):
    """Hold a process-wide lock so only one managed worker uses this runtime."""
    lock_path = Path(runtime_dir) / "managed-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError) as exc:
        handle.close()
        raise RuntimeError("Managed worker lain sudah memakai runtime ini; hentikan worker duplikat terlebih dahulu.") from exc
    return handle


def release_worker_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Config worker tidak dapat dibaca: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("Config worker harus berupa object JSON")
    api_base = config.get("api_base_url")
    parsed_api = urlsplit(api_base) if isinstance(api_base, str) else None
    secure_api = bool(
        parsed_api
        and parsed_api.hostname
        and not parsed_api.username
        and not parsed_api.password
        and not parsed_api.query
        and not parsed_api.fragment
        and (
            parsed_api.scheme == "https"
            or (parsed_api.scheme == "http" and parsed_api.hostname in {"localhost", "127.0.0.1"})
        )
    )
    if not secure_api:
        raise ValueError("api_base_url harus memakai HTTPS (HTTP hanya di localhost)")
    if not isinstance(config.get("worker_api_key_env"), str) or not config["worker_api_key_env"].strip():
        raise ValueError("worker_api_key_env wajib diisi")
    for key in ("master_config", "terminal_template_dir", "runtime_dir"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"Config worker wajib memiliki {key}")
    config["_config_dir"] = str(config_path.parent)
    config["_api_base"] = api_base.rstrip("/")
    config["_master_config"] = str(_resolve(config_path.parent, config["master_config"]))
    config["_terminal_template_dir"] = str(_resolve(config_path.parent, config["terminal_template_dir"]))
    config["_runtime_dir"] = str(_resolve(config_path.parent, config["runtime_dir"]))
    config["poll_seconds"] = max(2, min(60, int(config.get("poll_seconds", 5))))
    config["heartbeat_timeout_seconds"] = max(15, min(180, int(config.get("heartbeat_timeout_seconds", 45))))
    return config


@dataclass
class ManagedChild:
    account_id: int
    fingerprint: str
    process: Any
    heartbeat: Path
    started_at: float
    last_status: str = "STARTING"


class ManagedWorker:
    def __init__(self, config: dict[str, Any], *, popen: Any = subprocess.Popen, opener: Any = urlopen):
        if os.name != "nt":
            raise RuntimeError("Managed copier wajib berjalan pada Windows karena memakai terminal MT5 dan package MetaTrader5")
        self.config = config
        self.popen = popen
        self.opener = opener
        self.config_dir = Path(config["_config_dir"])
        self.runtime_dir = Path(config["_runtime_dir"])
        self.template_dir = Path(config["_terminal_template_dir"])
        self.master_config = Path(config["_master_config"])
        self.api_key = os.environ.get(config["worker_api_key_env"], "")
        self.parent_environment = os.environ.copy()
        if len(self.api_key) < 32:
            raise ValueError(f"Environment variable {config['worker_api_key_env']} harus berisi worker key minimal 32 karakter")
        template_terminal = self.template_dir / "terminal64.exe"
        if not template_terminal.is_file():
            raise ValueError("terminal_template_dir wajib menunjuk folder instalasi MT5 yang berisi terminal64.exe")
        master = json.loads(self.master_config.read_text(encoding="utf-8"))
        if not isinstance(master, dict) or not master.get("terminal_path") or not master.get("folder_sinyal"):
            raise ValueError("master_config harus mempunyai terminal_path dan folder_sinyal")
        self.master_password_env = str(master.get("password_env", ""))
        self.signal_dir = str(master["folder_sinyal"])
        self.children: dict[int, ManagedChild] = {}
        self.retry_at: dict[int, float] = {}
        self.failure_counts: dict[int, int] = {}
        self.master_process: Any = None
        self.master_retry_at = 0.0
        self.stopping = False
        self.logs_dir = self.runtime_dir / "logs"
        self.configs_dir = self.runtime_dir / "configs"
        self.terminals_dir = self.runtime_dir / "terminals"
        self.heartbeats_dir = self.runtime_dir / "heartbeats"
        self.reports_dir = self.runtime_dir / "reports"
        self.states_dir = self.runtime_dir / "states"
        for directory in (self.logs_dir, self.configs_dir, self.terminals_dir, self.heartbeats_dir, self.reports_dir, self.states_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _request(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.config['_api_base']}{endpoint}",
            data=body,
            headers={
                "Accept": "application/json",
                "X-Copier-Worker-Key": self.api_key,
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method="GET" if body is None else "POST",
        )
        try:
            with self.opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"API worker gagal (HTTP {exc.code} pada {endpoint})") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"API worker gagal ({type(exc).__name__})") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Respons API worker tidak valid")
        return result

    @staticmethod
    def _fingerprint(job: dict[str, Any]) -> str:
        raw = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _clean_environment(self) -> dict[str, str]:
        sensitive_markers = ("PASSWORD", "PASS_", "SECRET", "TOKEN", "API_KEY", "APIKEY")
        return {
            key: value for key, value in self.parent_environment.items()
            if key != self.config["worker_api_key_env"]
            and not any(marker in key.upper() for marker in sensitive_markers)
        }

    def _report(self, account_id: int, status: str, message: str | None = None) -> None:
        try:
            self._request(f"/api/mt5/copier/jobs/{account_id}/status", {"status": status, "message": message})
        except RuntimeError as exc:
            logger.warning("Status akun %s tidak terkirim: %s", account_id, exc)

    def _start_master(self, now: float) -> None:
        if self.master_process is not None and self.master_process.poll() is None:
            return
        if now < self.master_retry_at:
            return
        env = self._clean_environment()
        if self.master_password_env and self.master_password_env in self.parent_environment:
            env[self.master_password_env] = self.parent_environment[self.master_password_env]
        source_dir = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = source_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        log = (self.logs_dir / "master-publisher.log").open("ab")
        try:
            options = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            self.master_process = self.popen(
                [sys.executable, "-m", "copytrade.master_publisher", str(self.master_config)],
                cwd=Path(__file__).resolve().parents[2], env=env, stdout=log, stderr=subprocess.STDOUT, **options,
            )
            self.master_retry_at = now + 5
            logger.info("Master publisher dimulai pid=%s", self.master_process.pid)
        finally:
            log.close()

    def _make_job_config(self, job: dict[str, Any]) -> tuple[Path, dict[str, str], Path]:
        account_id = int(job["account_id"])
        if account_id <= 0:
            raise ValueError("ID akun worker tidak valid")
        name = f"managed_{account_id}"
        account_dir = self.terminals_dir / str(account_id)
        terminal = account_dir / "terminal64.exe"
        if not terminal.is_file():
            # A previous interrupted copy (or another worker startup) may have
            # created the account folder already. Complete that clone instead
            # of failing with FileExistsError on every retry.
            shutil.copytree(self.template_dir, account_dir, dirs_exist_ok=True)
        if not terminal.is_file():
            raise RuntimeError("Salinan terminal follower tidak lengkap")
        enable_terminal_auto_trading(account_dir)
        password_env = f"ALGENTRA_MANAGED_{account_id}_PASSWORD"
        config_path = self.configs_dir / f"{name}.json"
        heartbeat_dir = self.heartbeats_dir
        follower_config = {
            "nama": name,
            "terminal_path": str(terminal),
            "portable": True,
            "login": int(job["login"]),
            "password_env": password_env,
            "server": str(job["server"]),
            "folder_sinyal": self.signal_dir,
            "map_simbol": {"XAUUSD": str(job["follower_symbol"])},
            "mode_lot": job["mode_lot"],
            "rasio_lot": float(job["ratio_lot"]),
            "lot_tetap": float(job["lot_tetap"]),
            "saldo_master": 10000.0,
            "max_lot_per_order": float(job["max_lot_per_order"]),
            "max_umur_sinyal_buka_detik": 3,
            "deviation_poin": 20,
            "magic": 880001,
            "komentar": "Algentra managed copy",
            "arah_terbalik": False,
            "polling_ms": 50,
            "aktif": True,
            "dry_run": False,
            "max_posisi_terbuka": int(job["max_open_positions"]),
            "max_lot_total": float(job["max_lot_total"]),
            "izinkan_netting": False,
            "heartbeat_folder": str(heartbeat_dir),
            "state_path": str(self.states_dir / f"{account_id}.json"),
            "report_path": str(self.reports_dir / f"{account_id}.json"),
            "report_cursor_path": str(self.reports_dir / f"{account_id}.cursor.json"),
        }
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(follower_config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, config_path)
        env = self._clean_environment()
        env[password_env] = str(job["broker_password"])
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return config_path, env, self.heartbeats_dir / f"heartbeat_{name}.txt"

    def _validate_login(self, job: dict[str, Any]) -> tuple[bool, str]:
        """Check broker authentication in the isolated terminal without placing orders."""
        account_id = int(job["account_id"])
        account_dir = self.terminals_dir / str(account_id)
        terminal = account_dir / "terminal64.exe"
        try:
            if not terminal.is_file():
                # Login checks and job startup share the same persistent folder.
                # Make cloning idempotent so an existing partial folder is repaired.
                shutil.copytree(self.template_dir, account_dir, dirs_exist_ok=True)
            if not terminal.is_file():
                return False, "Terminal follower tidak tersedia pada server."
            enable_terminal_auto_trading(account_dir)
        except PermissionError as exc:
            logger.warning("Folder terminal akun %s sedang terkunci (%s)", account_id, exc)
            return False, "File terminal MT5 sedang dipakai proses lain. Tutup terminal MT5 untuk akun ini lalu coba simpan kredensial lagi."
        probe_env = self._clean_environment()
        source_dir = str(Path(__file__).resolve().parents[1])
        probe_env["PYTHONPATH"] = source_dir + (
            os.pathsep + probe_env["PYTHONPATH"] if probe_env.get("PYTHONPATH") else ""
        )
        probe_input = json.dumps({
            "path": str(terminal),
            "login": int(job["login"]),
            "password": str(job["broker_password"]),
            "server": str(job["server"]),
        })
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "copytrade.login_probe"],
                cwd=Path(__file__).resolve().parents[2],
                env=probe_env,
                input=probe_input,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Probe login MT5 akun %s melewati batas waktu 45 detik", account_id)
            return False, "Pemeriksaan MT5 melewati batas 45 detik (IPC timeout). Terminal tidak merespons; worker utama tetap berjalan."
        except OSError as exc:
            logger.warning("Probe login MT5 akun %s gagal dimulai (%s)", account_id, type(exc).__name__)
            return False, "Worker tidak dapat memulai pemeriksaan Python API MT5."
        if completed.returncode != 0:
            logger.warning("Probe login MT5 akun %s berhenti dengan kode %s", account_id, completed.returncode)
            return False, "Worker tidak dapat memvalidasi login pada terminal MT5."
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            logger.warning("Probe login MT5 akun %s menghasilkan respons tidak valid", account_id)
            return False, "Worker tidak dapat memvalidasi login pada terminal MT5."
        if not isinstance(result, dict) or not isinstance(result.get("valid"), bool) or not isinstance(result.get("message"), str):
            return False, "Worker tidak dapat memvalidasi login pada terminal MT5."
        return result["valid"], result["message"][:240]

    def _start_job(self, job: dict[str, Any], now: float) -> ManagedChild:
        account_id = int(job["account_id"])
        config_path, env, heartbeat = self._make_job_config(job)
        log = (self.logs_dir / f"managed_{account_id}.log").open("ab")
        try:
            options = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            process = self.popen(
                [sys.executable, "-m", "copytrade.follower", str(config_path)],
                cwd=Path(__file__).resolve().parents[2], env=env, stdout=log, stderr=subprocess.STDOUT, **options,
            )
        finally:
            log.close()
        child = ManagedChild(account_id, self._fingerprint(job), process, heartbeat, now)
        self.children[account_id] = child
        self._report(account_id, "STARTING")
        logger.info("Follower managed untuk akun %s dimulai pid=%s", account_id, process.pid)
        return child

    @staticmethod
    def _stop(process: Any) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))
        except (AttributeError, OSError, ValueError):
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _stop_job(self, account_id: int, *, report: bool = True) -> None:
        child = self.children.pop(account_id, None)
        if child is None:
            return
        self._stop(child.process)
        if report:
            self._report(account_id, "STOPPED")

    def _remove_orphan_runtime(self, account_id: int) -> None:
        """Remove only this worker's generated files for an account deleted in the portal."""
        safe_id = str(account_id)
        terminal_dir = (self.terminals_dir / safe_id).resolve()
        if terminal_dir.parent != self.terminals_dir.resolve():
            raise RuntimeError("Path cleanup akun berada di luar runtime worker")
        if terminal_dir.is_dir():
            shutil.rmtree(terminal_dir)
        for directory, filename in (
            (self.configs_dir, f"managed_{account_id}.json"),
            (self.states_dir, f"{account_id}.json"),
            (self.heartbeats_dir, f"heartbeat_managed_{account_id}.txt"),
            (self.reports_dir, f"{account_id}.json"),
            (self.reports_dir, f"{account_id}.cursor.json"),
            (self.logs_dir, f"managed_{account_id}.log"),
        ):
            candidate = (directory / filename).resolve()
            if candidate.parent != directory.resolve():
                raise RuntimeError("File cleanup akun berada di luar runtime worker")
            candidate.unlink(missing_ok=True)

    def sync_once(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._start_master(now)
        response = self._request("/api/mt5/copier/jobs")
        jobs = response.get("jobs")
        if not isinstance(jobs, list):
            raise RuntimeError("API worker tidak mengembalikan daftar jobs")
        managed_account_ids = response.get("managed_account_ids")
        if not isinstance(managed_account_ids, list) or any(not isinstance(value, int) or value <= 0 for value in managed_account_ids):
            raise RuntimeError("API worker tidak mengembalikan daftar akun managed yang valid")
        login_checks = response.get("login_checks", [])
        if not isinstance(login_checks, list):
            raise RuntimeError("API worker tidak mengembalikan daftar pemeriksaan login yang valid")
        for check in login_checks:
            if not isinstance(check, dict):
                continue
            account_id = int(check.get("account_id", 0))
            if account_id <= 0:
                continue
            # A follower and the login-check code must never initialize the
            # same portable terminal at the same time. The API marks the
            # profile inactive before queuing a check, but this worker may
            # still have its previous child alive until this poll completes.
            # Stop and reap that child before calling MT5's Python IPC API.
            self._stop_job(account_id, report=False)
            try:
                valid, message = self._validate_login(check)
            except Exception as exc:
                logger.exception("Pemeriksaan login MT5 akun %s gagal", account_id)
                valid, message = False, f"Pemeriksaan login gagal ({type(exc).__name__})."
            try:
                self._request(
                    f"/api/mt5/copier/jobs/{account_id}/login-check",
                    {"valid": valid, "message": message},
                )
            except RuntimeError as exc:
                logger.warning("Hasil pemeriksaan login akun %s tidak terkirim: %s", account_id, exc)
        desired: dict[int, dict[str, Any]] = {}
        used_accounts: set[tuple[str, str]] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            account_id = int(job.get("account_id", 0))
            key = (str(job.get("server", "")).casefold(), str(job.get("login", "")))
            if key in used_accounts:
                self._report(account_id, "ERROR", "Akun broker duplikat di daftar worker.")
                continue
            used_accounts.add(key)
            desired[account_id] = job

        for account_id in list(self.children):
            if account_id not in desired:
                self._stop_job(account_id)

        for account_dir in self.terminals_dir.iterdir():
            if account_dir.is_dir() and account_dir.name.isdigit() and int(account_dir.name) not in managed_account_ids:
                self._remove_orphan_runtime(int(account_dir.name))

        for account_id, job in desired.items():
            child = self.children.get(account_id)
            fingerprint = self._fingerprint(job)
            if child is not None and child.fingerprint != fingerprint:
                self._stop_job(account_id, report=False)
                child = None
            if child is None:
                if now < self.retry_at.get(account_id, 0.0):
                    self._report(account_id, "ERROR", "Follower gagal mulai; worker sedang menunggu sebelum mencoba lagi.")
                    continue
                try:
                    child = self._start_job(job, now)
                except Exception as exc:
                    logger.exception("Gagal menyiapkan follower managed akun %s", account_id)
                    self.retry_at[account_id] = now + 10
                    self._report(account_id, "ERROR", f"Persiapan follower gagal ({type(exc).__name__}).")
                    continue
            if child.process.poll() is not None:
                failures = self.failure_counts.get(account_id, 0) + 1
                self.failure_counts[account_id] = failures
                self.retry_at[account_id] = now + min(60, 2 ** min(failures, 6))
                self._stop_job(account_id, report=False)
                self._report(account_id, "ERROR", "Proses follower berhenti; worker akan mencoba lagi.")
                continue
            try:
                heartbeat_age = time.time() - child.heartbeat.stat().st_mtime
            except OSError:
                heartbeat_age = float("inf")
            if heartbeat_age <= self.config["heartbeat_timeout_seconds"]:
                self.failure_counts.pop(account_id, None)
                self._report(account_id, "RUNNING")
                report_path = self.reports_dir / f"{account_id}.json"
                try:
                    if time.time() - report_path.stat().st_mtime <= self.config["heartbeat_timeout_seconds"]:
                        account_report = json.loads(report_path.read_text(encoding="utf-8"))
                        if isinstance(account_report, dict):
                            scan_msc = int(account_report.pop("history_scan_msc", 0) or 0)
                            result = self._request(f"/api/mt5/copier/jobs/{account_id}/report", account_report)
                            cursor_path = self.reports_dir / f"{account_id}.cursor.json"
                            temporary = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
                            temporary.write_text(json.dumps({
                                "history_cursor": result.get("history_cursor", "0"),
                                "history_cursor_msc": result.get("history_cursor_msc", 0),
                                "scan_msc": scan_msc,
                            }), encoding="utf-8")
                            os.replace(temporary, cursor_path)
                except (OSError, json.JSONDecodeError, RuntimeError) as exc:
                    logger.warning("Laporan akun %s tidak tersinkron (%s)", account_id, type(exc).__name__)
            elif now - child.started_at < self.config["heartbeat_timeout_seconds"]:
                self._report(account_id, "STARTING")
            else:
                self._report(account_id, "ERROR", "Follower belum terhubung ke terminal MT5.")

    def run(self) -> None:
        logger.info("Managed copier worker dimulai")
        try:
            while not self.stopping:
                try:
                    self.sync_once()
                except RuntimeError as exc:
                    logger.warning("Sinkronisasi managed copier gagal: %s", exc)
                time.sleep(self.config["poll_seconds"])
        except KeyboardInterrupt:
            logger.info("Ctrl+C diterima")
        finally:
            self.stopping = True
            for account_id in list(self.children):
                self._stop_job(account_id)
            self._stop(self.master_process)
            logger.info("Managed copier worker berhenti")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="config/managed-worker.json")
    args = parser.parse_args()
    global logger
    logger = configure_logging("managed_worker")
    worker = ManagedWorker(load_config(args.config))
    lock = acquire_worker_lock(worker.runtime_dir)
    try:
        worker.run()
    finally:
        release_worker_lock(lock)


if __name__ == "__main__":
    main()
