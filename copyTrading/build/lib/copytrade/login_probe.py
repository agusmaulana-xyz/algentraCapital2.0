"""Probe a broker login with an isolated MT5 terminal and the login EA."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .mt5_runtime import disable_terminal_mcp, stop_terminals_in

PROBE_TIMEOUT_SECONDS = 150
STATUS_FILE = "algentra-login-probe.json"
STATUS_MARKER = "[MT5LoginProbe] STATUS "


def _result(valid: bool, message: str) -> None:
    sys.stdout.write(json.dumps({"valid": valid, "message": message}, ensure_ascii=False))
    sys.stdout.flush()


def _valid_access_point(value: str) -> bool:
    try:
        parsed = urlsplit("//" + value.strip())
        return bool(parsed.hostname and parsed.port and not parsed.username and not parsed.password and not parsed.path)
    except ValueError:
        return False


def _broker_access_point(server: str) -> str | None:
    endpoint = os.environ.get("MT5_BROKER_DIRECTORY_URL", "https://mt5.mtapi.io/Search").strip()
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        request = Request(
            endpoint + "?" + urlencode({"company": server}),
            headers={"Accept": "application/json"},
        )
        with urlopen(request, timeout=8) as response:
            companies = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
        if not isinstance(companies, list):
            return None
        for company in companies:
            if not isinstance(company, dict):
                continue
            for candidate in company.get("results", []):
                if not isinstance(candidate, dict) or str(candidate.get("name", "")).strip().casefold() != server.casefold():
                    continue
                for access in candidate.get("access", []):
                    if isinstance(access, str) and _valid_access_point(access):
                        return access.strip()
    except (OSError, URLError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def _normalized_windows_path(value: str) -> str:
    return str(value).strip().strip("\x00").replace("/", "\\").casefold().rstrip("\\")


def _seed_broker_servers(
    account_dir: Path,
    template_dir: Path,
    master_terminal_path: Path | None = None,
) -> None:
    target = account_dir / "Config" / "servers.dat"
    if target.is_file() and target.stat().st_size:
        return

    candidates: list[Path] = []
    if master_terminal_path is not None:
        master_dir = master_terminal_path.parent
        candidates.extend((
            master_dir / "Config" / "servers.dat",
            master_dir / "config" / "servers.dat",
        ))

    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata) / "MetaQuotes" / "Terminal"
        if master_terminal_path is not None:
            expected = _normalized_windows_path(str(master_terminal_path))
            try:
                for origin_path in root.glob("*/origin.txt"):
                    try:
                        origin = origin_path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    if _normalized_windows_path(origin) == expected:
                        candidates.append(origin_path.parent / "config" / "servers.dat")
            except OSError:
                pass
        try:
            profiles = list(root.glob("*/config/servers.dat"))
        except OSError:
            profiles = []
        profiles_with_mtime: list[tuple[float, Path]] = []
        for path in profiles:
            try:
                profiles_with_mtime.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.extend(path for _, path in sorted(profiles_with_mtime, reverse=True))

    candidates.extend((
        template_dir / "Config" / "servers.dat",
        template_dir / "config" / "servers.dat",
    ))
    for source in candidates:
        try:
            if source.is_file() and source.stat().st_size:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.resolve() == target.resolve():
                    return
                shutil.copy2(source, target)
                return
        except OSError:
            continue


def _read_status(paths: list[Path], log_offsets: dict[Path, int]) -> tuple[dict[str, Any] | None, str | None]:
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("ready"), bool):
                return data, None
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass  # MT5 can be midway through replacing the status file.

    for pattern in ("logs/*.log", "MQL5/Logs/*.log"):
        for log_path in paths[0].parents[2].glob(pattern):
            if log_path not in log_offsets:
                log_offsets[log_path] = 0

    failure: str | None = None
    for log_path, offset in list(log_offsets.items()):
        try:
            raw = log_path.read_bytes()
            if offset > len(raw):
                offset = 0
            chunk = raw[offset & ~1 :]
            log_offsets[log_path] = len(raw)
            text = chunk.decode("utf-16-le", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            marker_at = line.find(STATUS_MARKER)
            if marker_at >= 0:
                try:
                    data = json.loads(line[marker_at + len(STATUS_MARKER) :].strip())
                    if isinstance(data, dict) and isinstance(data.get("ready"), bool):
                        return data, None
                except json.JSONDecodeError:
                    pass
            lower = line.casefold()
            if "authorization on" in lower and "failed (" in lower:
                failure = lower.rsplit("failed (", 1)[-1].rstrip(") .\t\r\n")
            if "trading has been disabled" in lower and "disabled on server" in lower:
                failure = "trading disabled on server"
    return None, failure


def _auth_failure_message(reason: str) -> str:
    lowered = reason.casefold()
    if "invalid account" in lowered:
        return "Login ditolak broker (Invalid account). Periksa nomor login, password, dan nama server."
    if "account disabled" in lowered:
        return "Akun dinonaktifkan oleh broker."
    if "trading disabled on server" in lowered:
        return (
            "Login broker berhasil, tetapi server broker menonaktifkan trading untuk akun ini. "
            "Minta broker mengaktifkan izin trading akun sebelum copy trading dapat berjalan."
        )
    if "no connection" in lowered:
        return "Terminal tidak dapat terhubung ke server broker. Periksa nama server dan akses internet worker."
    return f"Broker menolak login: {reason}."[:240]


def _prepare_hidden_startup() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"startupinfo": startup, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _probe(request: dict[str, Any]) -> tuple[bool, str]:
    try:
        terminal = Path(str(request["path"])).resolve()
        login = int(request["login"])
        password = str(request["password"])
        server = str(request["server"]).strip()
    except (KeyError, TypeError, ValueError, OSError):
        return False, "Data akun MT5 untuk pemeriksaan tidak valid."
    if login <= 0:
        return False, "Nomor login MT5 harus berupa angka positif."
    if not server or not password or re.search(r"[\r\n\x00]", server + password):
        return False, "Server atau password broker kosong atau mengandung karakter tidak valid."
    if not terminal.is_file() or terminal.name.casefold() != "terminal64.exe":
        return False, "Terminal follower tidak tersedia pada server."

    account_dir = terminal.parent
    project_root = Path(__file__).resolve().parents[2]
    template_dir = Path(str(request.get("template_dir", ""))).resolve() if request.get("template_dir") else account_dir
    expert_source = project_root / "mt5" / "experts" / "Algentra" / "MT5LoginProbe.ex5"
    expert_target = account_dir / "MQL5" / "Experts" / "Algentra" / "MT5LoginProbe.ex5"
    try:
        if not expert_source.is_file():
            return False, "Expert MT5LoginProbe.ex5 belum dikompilasi pada server."
        expert_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(expert_source, expert_target)
        master_terminal = request.get("master_terminal_path")
        master_terminal_path = Path(str(master_terminal)).resolve() if master_terminal else None
        _seed_broker_servers(account_dir, template_dir, master_terminal_path)
        disable_terminal_mcp(account_dir)
        files_dir = account_dir / "MQL5" / "Files"
        files_dir.mkdir(parents=True, exist_ok=True)
        status_path = files_dir / STATUS_FILE
        status_path.unlink(missing_ok=True)
    except OSError as exc:
        return False, f"Worker tidak dapat menyiapkan terminal MT5 ({type(exc).__name__})."

    access_point = _broker_access_point(server) or server
    logs_dir = account_dir / "logs"
    try:
        log_offsets = {path: path.stat().st_size for path in logs_dir.glob("*.log")}
    except OSError:
        log_offsets = {}

    temp_dir = Path(tempfile.mkdtemp(prefix="algentra-mt5-login-"))
    ini_path = temp_dir / "login-probe.ini"
    ini = "\r\n".join((
        "[Common]",
        f"Login={login}",
        f"Password={password}",
        f"Server={access_point}",
        "KeepPrivate=1",
        "NewsEnable=0",
        "ProxyEnable=0",
        "",
        "[Experts]",
        "AllowLiveTrading=1",
        "AllowDllImport=0",
        "Enabled=1",
        "Account=0",
        "Profile=0",
        "",
        "[StartUp]",
        r"Expert=Algentra\MT5LoginProbe",
        "Symbol=XAUUSD",
        "Period=M1",
        "",
    ))
    try:
        ini_path.write_text(ini, encoding="utf-8", newline="")
        try:
            os.chmod(temp_dir, 0o700)
            os.chmod(ini_path, 0o600)
        except OSError:
            pass
        stop_terminals_in(account_dir)
        try:
            process = subprocess.Popen(
                [str(terminal), "/portable", f"/config:{ini_path}"],
                cwd=account_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=os.environ.copy(),
                **_prepare_hidden_startup(),
            )
        except OSError:
            return False, "Terminal MT5 tidak dapat dijalankan oleh worker."

        remove_timer = threading.Timer(20, lambda: ini_path.unlink(missing_ok=True))
        remove_timer.daemon = True
        remove_timer.start()
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        last_status: dict[str, Any] | None = None
        last_auth_failure: str | None = None
        try:
            while time.monotonic() < deadline:
                status, auth_failure = _read_status([status_path], log_offsets)
                if auth_failure:
                    last_auth_failure = auth_failure
                if status is not None:
                    last_status = status
                    if status.get("ready"):
                        if int(status.get("login", 0) or 0) != login:
                            return False, "Terminal login ke akun lain dari yang didaftarkan."
                        if str(status.get("server", "")).strip().casefold() != server.casefold():
                            return False, "Server broker yang terhubung tidak cocok dengan pilihan akun."
                        valid = status.get("valid")
                        message = status.get("message")
                        if isinstance(valid, bool) and isinstance(message, str):
                            return valid, message[:240]
                        return False, "Terminal MT5 mengirim status login yang tidak valid."
                if process.poll() is not None:
                    if last_auth_failure:
                        return False, _auth_failure_message(last_auth_failure)
                    return False, "Terminal MT5 berhenti sebelum hasil login diterima. Periksa konfigurasi terminal."
                time.sleep(0.5)
        finally:
            remove_timer.cancel()
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
            stop_terminals_in(account_dir)

        if last_auth_failure:
            return False, _auth_failure_message(last_auth_failure)
        if last_status and isinstance(last_status.get("message"), str):
            detail = last_status["message"].strip()
            if detail and "menunggu koneksi broker" not in detail.casefold():
                return False, detail[:240]
            if detail:
                return False, "Terminal melaporkan belum terhubung ke broker setelah 150 detik. Periksa server broker dan koneksi worker."
        return False, "Terminal MT5 tidak mengirim laporan EA dalam 150 detik. Login terminal bisa saja berhasil, tetapi EA pemeriksa belum aktif. Periksa jurnal MT5 dan folder MQL5\\Files."
    finally:
        try:
            ini_path.unlink(missing_ok=True)
            temp_dir.rmdir()
        except OSError:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        _result(False, "Data pemeriksaan login tidak dapat dibaca worker.")
        return
    if not isinstance(request, dict):
        _result(False, "Data pemeriksaan login tidak valid.")
        return
    try:
        valid, message = _probe(request)
    except Exception as exc:
        _result(False, f"Pemeriksaan login gagal ({type(exc).__name__}).")
        return
    _result(valid, message)


if __name__ == "__main__":
    main()
