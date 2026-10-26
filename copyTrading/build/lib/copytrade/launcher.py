"""Watchdog that supervises the publisher and independent follower processes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .follower import validate_config as validate_follower_config
from .logging_setup import configure_logging

logger = logging.getLogger("launcher")
BACKOFF_SECONDS = (1, 2, 5, 10, 30)


@dataclass
class Child:
    name: str
    command: list[str]
    heartbeat: Path | None = None
    process: Any = None
    started_at: float = 0.0
    next_start_at: float = 0.0
    restart_times: deque[float] = field(default_factory=deque)
    permanent_failure: bool = False


class Supervisor:
    def __init__(self, config_path: str | Path, config: dict[str, Any], *, popen: Any = subprocess.Popen,
                 python: str | None = None, project_root: str | Path | None = None):
        self.config_path = Path(config_path).resolve()
        self.config = config
        self.config_dir = self.config_path.parent
        self.project_root = Path(project_root or self.config_dir.parent).resolve()
        self.python = python or sys.executable
        self.popen = popen
        self.check_seconds = max(1, int(config.get("check_interval_seconds", 5)))
        self.heartbeat_timeout = max(5, int(config.get("heartbeat_timeout_seconds", 30)))
        self.max_restarts = max(1, int(config.get("max_restarts_per_10_minutes", 10)))
        self.logger = logger
        self.children = self._make_children()
        self.last_status = 0.0
        self.stopping = False

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.config_dir / path

    def _command(self, module: str, config: Path) -> list[str]:
        return [self.python, "-m", module, str(config.resolve())]

    def _make_children(self) -> list[Child]:
        master_path = self._resolve(str(self.config["master_config"]))
        children = [Child("master", self._command("copytrade.master_publisher", master_path))]
        master_config = json.loads(master_path.read_text(encoding="utf-8"))
        if not isinstance(master_config, dict) or not master_config.get("terminal_path") or not master_config.get("folder_sinyal"):
            raise ValueError(f"Config master {master_path} wajib memiliki terminal_path dan folder_sinyal")
        terminal_paths: set[str] = set()
        if master_config.get("terminal_path"):
            terminal_paths.add(os.path.normcase(os.path.abspath(str(master_config["terminal_path"]))))
        logins: set[str] = set()
        names = {"master"}
        for follower_value in self.config.get("followers", []):
            follower_path = self._resolve(str(follower_value))
            follower_config = json.loads(follower_path.read_text(encoding="utf-8"))
            if not isinstance(follower_config, dict):
                raise ValueError(f"Config follower {follower_path} harus berupa object JSON")
            active = follower_config.get("aktif", True)
            if not isinstance(active, bool):
                raise ValueError(f"Config follower field 'aktif' harus boolean JSON: {follower_path}")
            if not active:
                continue
            follower_config = validate_follower_config(follower_config)
            if not isinstance(follower_config.get("nama"), str) or not follower_config["nama"].strip():
                raise ValueError(f"Config follower {follower_path} wajib memiliki nama string")
            name = follower_config["nama"]
            if name in names:
                raise ValueError(f"Nama proses follower duplikat: {name}")
            names.add(name)
            if "terminal_path" not in follower_config or "login" not in follower_config:
                raise ValueError(f"Config follower {follower_path} wajib memiliki terminal_path dan login")
            terminal_path = os.path.normcase(os.path.abspath(str(follower_config["terminal_path"])))
            if terminal_path in terminal_paths:
                raise ValueError(f"terminal_path dipakai lebih dari satu proses: {follower_config['terminal_path']}")
            terminal_paths.add(terminal_path)
            login = str(follower_config["login"])
            if login in logins:
                raise ValueError(f"Akun follower login {login} dipakai lebih dari satu proses")
            logins.add(login)
            heartbeat_dir = Path(follower_config.get("heartbeat_folder", self.project_root))
            if not heartbeat_dir.is_absolute():
                heartbeat_dir = self.project_root / heartbeat_dir
            children.append(Child(name, self._command("copytrade.follower", follower_path), heartbeat_dir / f"heartbeat_{name}.txt"))
        return children

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        src = str(self.project_root / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def _start(self, child: Child) -> None:
        if child.permanent_failure or self.stopping:
            return
        options = {}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        child.process = self.popen(child.command, cwd=self.project_root, env=self._environment(), **options)
        child.started_at = time.monotonic()
        self.logger.info("Proses %s dimulai pid=%s", child.name, child.process.pid)

    def start_all(self) -> None:
        for child in self.children:
            self._start(child)

    def _stop_child(self, child: Child) -> None:
        process = child.process
        if process is None or process.poll() is not None:
            child.process = None
            return
        try:
            control_signal = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT) if os.name == "nt" else signal.SIGINT
            process.send_signal(control_signal)
        except (AttributeError, OSError, ValueError):
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        child.process = None

    def _heartbeat_stale(self, child: Child, now: float) -> bool:
        if child.heartbeat is None or now - child.started_at <= self.heartbeat_timeout:
            return False
        try:
            age = time.time() - child.heartbeat.stat().st_mtime
        except OSError:
            age = float("inf")
        return age > self.heartbeat_timeout

    def _schedule_restart(self, child: Child, now: float, reason: str) -> None:
        self._stop_child(child)
        cutoff = now - 600
        while child.restart_times and child.restart_times[0] < cutoff:
            child.restart_times.popleft()
        if len(child.restart_times) >= self.max_restarts:
            child.permanent_failure = True
            self.logger.critical("Proses %s gagal permanen setelah %d restart/10 menit (%s)", child.name, len(child.restart_times), reason)
            return
        child.restart_times.append(now)
        delay = BACKOFF_SECONDS[min(len(child.restart_times) - 1, len(BACKOFF_SECONDS) - 1)]
        child.next_start_at = now + delay
        self.logger.error("Proses %s akan dimulai ulang dalam %ds: %s", child.name, delay, reason)

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for child in self.children:
            if child.permanent_failure:
                continue
            if child.process is None:
                if now >= child.next_start_at:
                    self._start(child)
                continue
            exit_code = child.process.poll()
            if exit_code is not None:
                child.process = None
                self._schedule_restart(child, now, f"exit code {exit_code}")
                continue
            if self._heartbeat_stale(child, now):
                self._schedule_restart(child, now, "heartbeat lebih tua dari batas")
        if now - self.last_status >= 60:
            summary = ", ".join(f"{c.name}={'gagal permanen' if c.permanent_failure else 'berjalan' if c.process is not None else 'menunggu restart'}" for c in self.children)
            self.logger.info("Status proses: %s", summary)
            self.last_status = now

    def stop_all(self) -> None:
        self.stopping = True
        for child in self.children:
            self._stop_child(child)
        self.logger.info("Semua proses anak dihentikan")

    def run(self) -> None:
        try:
            self.start_all()
            while True:
                self.tick()
                time.sleep(self.check_seconds)
        except KeyboardInterrupt:
            self.logger.info("Ctrl+C diterima")
        finally:
            self.stop_all()


def load_config(path: str | Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not config.get("master_config") or not isinstance(config.get("followers"), list):
        raise ValueError("Config launcher wajib berisi master_config dan daftar followers")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="config/launcher.json")
    args = parser.parse_args()
    global logger
    logger = configure_logging("launcher")
    path = Path(args.config).resolve()
    Supervisor(path, load_config(path)).run()


if __name__ == "__main__":
    main()
