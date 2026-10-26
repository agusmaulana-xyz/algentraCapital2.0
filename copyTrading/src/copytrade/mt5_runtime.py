"""Windows MT5 runtime isolation helpers for managed terminals."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
from pathlib import Path


def disable_terminal_mcp(terminal_dir: str | Path) -> Path:
    """Disable MT5's optional localhost MCP listener in one isolated terminal."""
    terminal_dir = Path(terminal_dir)
    config_dir = terminal_dir / "Config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "assistant.ini"
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
    section_seen = False
    in_mt5_mcp = False
    enable_seen = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_mt5_mcp and not enable_seen:
                output.append("Enable=0")
                enable_seen = True
            in_mt5_mcp = stripped[1:-1].casefold() == "mcp.metatrader"
            section_seen = section_seen or in_mt5_mcp
            output.append(line)
            continue
        if in_mt5_mcp and "=" in stripped and not stripped.startswith((";", "#")):
            key = stripped.split("=", 1)[0].strip().casefold()
            if key in {"enable", "enabled"}:
                output.append("Enable=0")
                enable_seen = True
                continue
        output.append(line)
    if in_mt5_mcp and not enable_seen:
        output.append("Enable=0")
        enable_seen = True
    if not section_seen:
        if output and output[-1].strip():
            output.append("")
        output.extend(("[MCP.MetaTrader]", "Enable=0"))
    rendered = newline.join(output) + (newline if output else "")
    if rendered != text:
        temporary = config_path.with_name(f"{config_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temporary.write_bytes(rendered.encode(encoding))
            temporary.replace(config_path)
        finally:
            temporary.unlink(missing_ok=True)
    return config_path


def _terminal_process_is_running(terminal_path: str | Path) -> bool:
    if os.name != "nt":
        return False
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return False
    wanted = str(Path(terminal_path).resolve())
    script = (
        "$wanted = [System.IO.Path]::GetFullPath($env:ALGENTRA_TERMINAL_EXE); "
        "$found = Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
        "Where-Object { $_.ExecutablePath -and [System.String]::Equals(" 
        "$_.ExecutablePath, $wanted, [System.StringComparison]::OrdinalIgnoreCase) }; "
        "if ($found) { '1' } else { '0' }"
    )
    env = os.environ.copy()
    env["ALGENTRA_TERMINAL_EXE"] = wanted
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def start_portable_terminal(terminal_path: str | Path, *, timeout: float = 30.0) -> None:
    """Start this account's isolated portable terminal with Algo Trading enabled."""
    terminal = Path(terminal_path).resolve()
    if os.name != "nt" or not terminal.is_file():
        raise FileNotFoundError(f"Terminal MT5 tidak tersedia: {terminal}")
    if _terminal_process_is_running(terminal):
        return

    startup_config = terminal.parent / "Config" / "algentra-managed-startup.ini"
    startup_config.parent.mkdir(parents=True, exist_ok=True)
    startup_config.write_text(
        "[Common]\r\nKeepPrivate=1\r\nNewsEnable=0\r\nProxyEnable=0\r\n"
        "\r\n[Experts]\r\nAllowLiveTrading=1\r\nAllowDllImport=0\r\n"
        "Enabled=1\r\nAccount=0\r\nProfile=0\r\n",
        encoding="utf-8",
        newline="",
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen(
        [str(terminal), "/portable", f"/config:{startup_config}"],
        cwd=terminal.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startup,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if _terminal_process_is_running(terminal):
            return
        if process.poll() is not None:
            raise RuntimeError(f"Terminal MT5 follower berhenti saat startup (exit={process.returncode})")
        time.sleep(0.25)
    raise TimeoutError("Terminal MT5 follower tidak muncul sebelum batas waktu startup")


def restart_portable_terminal(terminal_path: str | Path, *, timeout: float = 30.0) -> None:
    """Recover an isolated managed terminal whose Python IPC endpoint is stuck."""
    terminal = Path(terminal_path).resolve()
    if os.name != "nt":
        raise OSError("Pemulihan terminal MT5 portable hanya didukung di Windows")
    if not terminal.is_file():
        raise FileNotFoundError(f"Terminal MT5 tidak tersedia: {terminal}")
    # Managed accounts use a private portable folder, so only processes launched
    # from this account's terminal directory are stopped. Broker credentials stay
    # in the terminal's saved session and are never passed to the restart command.
    stop_terminals_in(terminal.parent)
    start_portable_terminal(terminal, timeout=timeout)


def stop_terminals_in(terminal_dir: str | Path, *, timeout: int = 20) -> None:
    """Stop processes launched from one isolated MT5 terminal directory."""
    if os.name != "nt":
        return
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return
    resolved = str(Path(terminal_dir).resolve())
    script = (
        "$dir = $env:ALGENTRA_TERMINAL_DIR.TrimEnd('\\') + '\\'; "
        "$deadline = [DateTime]::UtcNow.AddSeconds([int]$env:ALGENTRA_TERMINAL_TIMEOUT_SECONDS); "
        "while ($true) { "
        "$processes = @(Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir, "
        "[System.StringComparison]::OrdinalIgnoreCase) }); "
        "if ($processes.Count -eq 0) { break }; "
        "$processes | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "if ([DateTime]::UtcNow -ge $deadline) { break }; Start-Sleep -Milliseconds 200 }"
    )
    env = os.environ.copy()
    env["ALGENTRA_TERMINAL_DIR"] = resolved
    env["ALGENTRA_TERMINAL_TIMEOUT_SECONDS"] = str(max(1, int(timeout)))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def remove_readonly_tree(path: str | Path) -> None:
    """Remove an ephemeral runtime tree, retrying transient Windows file locks."""
    target = Path(path)
    for attempt in range(5):
        if not target.exists():
            return
        for current, dirs, files in os.walk(target):
            for name in dirs + files:
                item = Path(current) / name
                try:
                    item.chmod(item.stat().st_mode | 0o200)
                except OSError:
                    pass
        try:
            target.chmod(target.stat().st_mode | 0o700)
        except OSError:
            pass
        try:
            shutil.rmtree(target)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.25 * (attempt + 1))

