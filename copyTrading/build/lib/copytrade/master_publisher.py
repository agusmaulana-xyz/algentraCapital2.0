"""Observe an already logged-in master terminal and publish position deltas."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .logging_setup import configure_logging
from .protocol import Signal
from .signal_io import append_signal

logger = logging.getLogger("master")


def _value(position: Any, name: str, default: Any = None) -> Any:
    if isinstance(position, Mapping):
        return position.get(name, default)
    return getattr(position, name, default)


def snapshot_positions(positions: Iterable[Any], config: Mapping[str, Any] | None = None, mt5: Any = None) -> dict[int, dict[str, Any]]:
    config = config or {}
    magic_filter = set(config.get("filter_magic") or [])
    symbol_filter = set(config.get("filter_simbol") or [])
    ignored = tuple(str(x).casefold() for x in (config.get("abaikan_komentar_mengandung") or []))
    result: dict[int, dict[str, Any]] = {}
    for pos in positions:
        magic = int(_value(pos, "magic", 0) or 0)
        symbol = str(_value(pos, "symbol", ""))
        comment = str(_value(pos, "comment", "") or "")
        if magic_filter and magic not in magic_filter:
            continue
        if symbol_filter and symbol not in symbol_filter:
            continue
        if ignored and any(part in comment.casefold() for part in ignored):
            continue
        identifier = int(_value(pos, "identifier", _value(pos, "ticket", 0)))
        raw_type = _value(pos, "type", 0)
        buy_type = getattr(mt5, "POSITION_TYPE_BUY", 0) if mt5 else 0
        result[identifier] = {
            "pos_id": identifier,
            "simbol": symbol,
            "arah": "buy" if raw_type == buy_type else "sell",
            "lot": float(_value(pos, "volume", 0.0)),
            "harga": float(_value(pos, "price_open", 0.0)),
            "sl": float(_value(pos, "sl", 0.0) or 0.0),
            "tp": float(_value(pos, "tp", 0.0) or 0.0),
            "komentar": comment,
            "magic": magic,
        }
    return result


def diff_snapshot(previous: Mapping[int, Mapping[str, Any]], current_positions: Iterable[Any] | None,
                  config: Mapping[str, Any] | None = None, mt5: Any = None) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]] | None:
    """Return changes and the next valid snapshot; None input means no update."""
    if current_positions is None:
        return None
    current = snapshot_positions(current_positions, config, mt5)
    events: list[dict[str, Any]] = []
    for pos_id, now in current.items():
        old = previous.get(pos_id)
        if old is None:
            events.append({"aksi": "buka", **{k: now[k] for k in ("pos_id", "simbol", "arah", "lot", "harga", "sl", "tp", "komentar")}})
            continue
        if now["sl"] != old["sl"] or now["tp"] != old["tp"]:
            events.append({"aksi": "ubah", "pos_id": pos_id, "sl": now["sl"], "tp": now["tp"]})
        if now["lot"] < old["lot"] - 1e-10:
            events.append({"aksi": "kurang", "pos_id": pos_id, "lot": old["lot"] - now["lot"], "lot_sisa": now["lot"]})
    for pos_id in previous.keys() - current.keys():
        events.append({"aksi": "tutup", "pos_id": pos_id})
    return events, current


@dataclass
class SignalIdGenerator:
    last_ms: int = 0
    counter: int = 0

    def next(self) -> str:
        now_ms = int(time.time() * 1000)
        if now_ms > self.last_ms:
            self.last_ms, self.counter = now_ms, 0
        else:
            self.counter += 1
        return f"{self.last_ms}-{self.counter}"


def _load_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Package MetaTrader5 hanya tersedia di Windows; instal di VPS Windows") from exc
    return mt5


def reconnect_master(mt5: Any, terminal_path: str) -> bool:
    """Reattach the publisher to its configured source terminal after disconnect."""
    mt5.shutdown()
    return bool(mt5.initialize(path=terminal_path, timeout=10_000))


def _master_terminal_is_running(terminal_path: str) -> bool:
    """Check the exact master executable before MT5 Python can launch it."""
    if os.name != "nt":
        return True
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return False
    script = (
        "$wanted = [System.IO.Path]::GetFullPath($env:ALGENTRA_MASTER_TERMINAL_PATH); "
        "$found = Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
        "Where-Object { $_.ExecutablePath -and [System.String]::Equals(" 
        "$_.ExecutablePath, $wanted, [System.StringComparison]::OrdinalIgnoreCase) }; "
        "if ($found) { '1' } else { '0' }"
    )
    env = os.environ.copy()
    env["ALGENTRA_MASTER_TERMINAL_PATH"] = terminal_path
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def run(config: Mapping[str, Any], mt5: Any | None = None, *, stop_event: Any = None) -> None:
    mt5 = mt5 or _load_mt5()
    output = Path(config["folder_sinyal"])
    poll = max(1, int(config.get("poll_interval_ms", 50))) / 1000
    terminal_path = str(config["terminal_path"])
    previous: dict[int, dict[str, Any]] | None = None
    ids = SignalIdGenerator()
    initialized = False
    reconnect_delay = 5.0
    reconnect_at = 0.0
    waiting_for_master_log = False
    try:
        while stop_event is None or not stop_event.is_set():
            now = time.monotonic()
            if not initialized:
                if now < reconnect_at:
                    time.sleep(poll)
                    continue
                if not _master_terminal_is_running(terminal_path):
                    if not waiting_for_master_log:
                        logger.warning("Terminal master belum dibuka; publisher menunggu terminal sumber tanpa menjalankannya")
                        waiting_for_master_log = True
                    reconnect_at = now + 5.0
                    time.sleep(poll)
                    continue
                waiting_for_master_log = False
                if not mt5.initialize(path=terminal_path, timeout=10_000):
                    error = mt5.last_error() if hasattr(mt5, "last_error") else None
                    logger.warning("Publisher gagal terhubung ke terminal master (%s)", error)
                    reconnect_at = now + reconnect_delay
                    reconnect_delay = min(60.0, reconnect_delay * 2)
                    time.sleep(poll)
                    continue
                initialized = True
                reconnect_delay = 5.0
                logger.info("Publisher terhubung ke terminal master %s", terminal_path)
            terminal = mt5.terminal_info()
            if terminal is None or not getattr(terminal, "connected", False):
                if now >= reconnect_at:
                    if not _master_terminal_is_running(terminal_path):
                        logger.warning("Terminal master ditutup; publisher menunggu terminal dibuka kembali")
                        mt5.shutdown()
                        initialized = False
                        reconnect_at = now + 5.0
                        waiting_for_master_log = True
                    else:
                        logger.warning("Terminal master terputus; mencoba menyambungkan ulang")
                        try:
                            reconnected = reconnect_master(mt5, terminal_path)
                            initialized = reconnected
                            current = mt5.terminal_info() if reconnected else None
                        except Exception:
                            logger.exception("Sambung ulang terminal master gagal")
                            current = None
                        if current is not None and getattr(current, "connected", False):
                            logger.info("Publisher kembali terhubung ke terminal master %s", terminal_path)
                            reconnect_delay = 5.0
                        else:
                            error = mt5.last_error() if hasattr(mt5, "last_error") else None
                            logger.warning("Terminal master belum tersambung; percobaan berikutnya dalam %.0f detik (%s)", reconnect_delay, error)
                            reconnect_delay = min(60.0, reconnect_delay * 2)
                        reconnect_at = now + reconnect_delay
                time.sleep(poll)
                continue
            positions = mt5.positions_get()
            if positions is None:
                logger.error("positions_get gagal: %s", mt5.last_error())
                time.sleep(poll)
                continue
            if previous is None:
                previous = snapshot_positions(positions, config, mt5)
                logger.info("Snapshot baseline awal berisi %d posisi", len(previous))
                time.sleep(poll)
                continue
            result = diff_snapshot(previous, positions, config, mt5)
            assert result is not None
            events, previous = result
            for event in events:
                now = time.time()
                signal = Signal(v=1, id=ids.next(), ts=now, aksi=event.pop("aksi"), **event)
                append_signal(output, signal)
                logger.info("Sinyal %s aksi=%s diterbitkan", signal.id, signal.aksi)
            time.sleep(poll)
    finally:
        if initialized:
            mt5.shutdown()


def load_config(path: str | Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not config.get("terminal_path") or not config.get("folder_sinyal"):
        raise ValueError("Config master wajib memiliki terminal_path dan folder_sinyal")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    args = parser.parse_args()
    global logger
    logger = configure_logging("master")
    try:
        run(load_config(args.config))
    except KeyboardInterrupt:
        logger.info("Publisher dihentikan")
    except Exception:
        logger.exception("Publisher berhenti karena kesalahan")
        raise


if __name__ == "__main__":
    main()
