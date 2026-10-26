import json
import subprocess
from types import SimpleNamespace

import pytest

from copytrade.managed_worker import (
    ManagedWorker,
    _resolve,
    acquire_worker_lock,
    enable_terminal_auto_trading,
    load_config,
    release_worker_lock,
)


def test_load_config_resolves_local_paths_and_accepts_https_worker_api(tmp_path):
    config_path = tmp_path / "managed.json"
    config_path.write_text(json.dumps({
        "api_base_url": "https://algentra.example",
        "worker_api_key_env": "COPIER_WORKER_API_KEY",
        "master_config": "config/master.json",
        "terminal_template_dir": "C:/MT5_Template",
        "runtime_dir": "runtime",
        "poll_seconds": 1,
    }), encoding="utf-8")

    config = load_config(config_path)

    assert config["_api_base"] == "https://algentra.example"
    assert config["_master_config"] == str((tmp_path / "config/master.json").resolve())
    assert config["_runtime_dir"] == str((tmp_path / "runtime").resolve())
    assert config["poll_seconds"] == 2


@pytest.mark.parametrize("api_base", [
    "http://algentra.example",
    "http://localhost.evil.example",
    "ftp://algentra.example",
    "https://user:password@algentra.example",
])
def test_load_config_rejects_untrusted_or_ambiguous_api_urls(tmp_path, api_base):
    path = tmp_path / "managed.json"
    path.write_text(json.dumps({
        "api_base_url": api_base,
        "worker_api_key_env": "COPIER_WORKER_API_KEY",
        "master_config": "master.json",
        "terminal_template_dir": "C:/MT5_Template",
        "runtime_dir": "runtime",
    }), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path)


def test_resolve_keeps_absolute_paths_and_resolves_relative_paths(tmp_path):
    assert _resolve(tmp_path, "runtime") == (tmp_path / "runtime").resolve()
    assert _resolve(tmp_path, str((tmp_path / "absolute").resolve())) == (tmp_path / "absolute").resolve()


def test_worker_runtime_lock_blocks_duplicate_worker(tmp_path):
    first = acquire_worker_lock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="worker lain"):
            acquire_worker_lock(tmp_path)
    finally:
        release_worker_lock(first)
    second = acquire_worker_lock(tmp_path)
    release_worker_lock(second)


def test_enable_terminal_auto_trading_updates_experts_section_idempotently(tmp_path):
    config = tmp_path / "Config" / "common.ini"
    config.parent.mkdir()
    config.write_text("[Charts]\nTradeHistory=1\n\n[Experts]\nAllowLiveTrading=0\nOther=2\n", encoding="utf-8")

    enable_terminal_auto_trading(tmp_path)
    enable_terminal_auto_trading(tmp_path)

    result = config.read_text(encoding="utf-8")
    assert "[Experts]\nAllowLiveTrading=1\nOther=2\nEnabled=1\nAccount=0\nProfile=0" in result
    assert result.count("AllowLiveTrading=") == 1
    assert result.count("Enabled=") == 1
    assert result.count("Account=") == 1
    assert result.count("Profile=") == 1


def test_enable_terminal_auto_trading_preserves_utf16_ini(tmp_path):
    config = tmp_path / "Config" / "common.ini"
    config.parent.mkdir()
    config.write_text("[Experts]\r\nAllowLiveTrading=0\r\n", encoding="utf-16")

    enable_terminal_auto_trading(tmp_path)

    assert "AllowLiveTrading=1" in config.read_text(encoding="utf-16")


def test_validate_login_returns_probe_success(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "terminal64.exe").write_bytes(b"test-terminal")
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.terminals_dir = tmp_path / "runtime" / "terminals"
    worker.template_dir = template
    worker.parent_environment = {}
    worker.config = {"worker_api_key_env": "COPIER_WORKER_API_KEY"}
    calls = []
    monkeypatch.setattr(
        "copytrade.managed_worker.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"valid": True, "message": "Login broker berhasil dan terminal siap untuk copy trading."}),
        ),
    )

    valid, message = worker._validate_login({
        "account_id": 8,
        "login": "12345",
        "server": "Broker-Demo",
        "broker_password": "private-password",
    })

    assert valid is True
    assert message == "Login broker berhasil dan terminal siap untuk copy trading."
    assert "private-password" in calls[0][1]["input"]
    assert "private-password" not in " ".join(calls[0][0])
    assert calls[0][1]["timeout"] == 165
    assert json.loads(calls[0][1]["input"])["template_dir"] == str(template)
    ini = (worker.terminals_dir / "8" / "Config" / "common.ini").read_text(encoding="utf-8")
    assert "AllowLiveTrading=1" in ini


def test_validate_login_repairs_existing_partial_terminal_folder(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "terminal64.exe").write_bytes(b"test-terminal")
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.terminals_dir = tmp_path / "runtime" / "terminals"
    worker.template_dir = template
    worker.parent_environment = {}
    worker.config = {"worker_api_key_env": "COPIER_WORKER_API_KEY"}
    partial = worker.terminals_dir / "8"
    partial.mkdir(parents=True)
    (partial / "stale.marker").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(
        "copytrade.managed_worker.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"valid": True, "message": "ok"}),
        ),
    )

    valid, _ = worker._validate_login({
        "account_id": 8,
        "login": "12345",
        "server": "Broker-Demo",
        "broker_password": "private-password",
    })

    assert valid is True
    assert (partial / "terminal64.exe").read_bytes() == b"test-terminal"


def test_validate_login_reports_mt5_connection_error_code_without_credentials(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "terminal64.exe").write_bytes(b"test-terminal")
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.terminals_dir = tmp_path / "runtime" / "terminals"
    worker.template_dir = template
    worker.parent_environment = {}
    worker.config = {"worker_api_key_env": "COPIER_WORKER_API_KEY"}
    monkeypatch.setattr(
        "copytrade.managed_worker.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"valid": False, "message": "Terminal MT5 gagal tersambung ke broker (kode -7: Terminal: connect failed). Periksa nama server broker dan koneksi jaringan VPS."}),
        ),
    )

    valid, message = worker._validate_login({
        "account_id": 9,
        "login": "12345",
        "server": "Broker-Demo",
        "broker_password": "private-password",
    })

    assert valid is False
    assert "kode -7: Terminal: connect failed" in message
    assert "private-password" not in message


def test_validate_login_returns_native_terminal_disconnect_without_credentials(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "terminal64.exe").write_bytes(b"test-terminal")
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.terminals_dir = tmp_path / "runtime" / "terminals"
    worker.template_dir = template
    worker.parent_environment = {}
    worker.config = {"worker_api_key_env": "COPIER_WORKER_API_KEY"}
    monkeypatch.setattr(
        "copytrade.managed_worker.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"valid": False, "message": "Terminal MT5 tidak tersambung ke server broker dalam 150 detik. Periksa nama server dan akses internet worker."}),
        ),
    )

    valid, message = worker._validate_login({
        "account_id": 10,
        "login": "12345",
        "server": "Broker-Demo",
        "broker_password": "private-password",
    })

    assert valid is False
    assert "tidak tersambung ke server broker" in message
    assert len(message) <= 240


def test_validate_login_bounds_a_hung_mt5_probe(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "terminal64.exe").write_bytes(b"test-terminal")
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.terminals_dir = tmp_path / "runtime" / "terminals"
    worker.template_dir = template
    worker.parent_environment = {}
    worker.config = {"worker_api_key_env": "COPIER_WORKER_API_KEY"}

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("login probe", timeout=165)

    monkeypatch.setattr("copytrade.managed_worker.subprocess.run", timeout)
    valid, message = worker._validate_login({
        "account_id": 11,
        "login": "12345",
        "server": "Broker-Demo",
        "broker_password": "private-password",
    })

    assert valid is False
    assert "165 detik" in message
    assert "worker utama tetap berjalan" in message


def test_sync_stops_existing_follower_before_validating_its_terminal(tmp_path):
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.children = {10: object()}
    worker.terminals_dir = tmp_path / "terminals"
    worker.terminals_dir.mkdir()
    worker.retry_at = {}
    worker.failure_counts = {}
    worker.login_check_retry_at = {}
    worker.api_missing_until = {}
    worker.api_missing_absent_accounts = set()
    worker.prepayment_retry_at = {}
    worker.orphan_session_since = {}
    events = []
    worker._start_master = lambda _now: None
    worker._request = lambda endpoint, payload=None: (
        {"jobs": [], "managed_account_ids": [10], "login_checks": [{"account_id": 10}]}
        if endpoint == "/api/mt5/copier/jobs"
        else {}
    )

    def stop_job(account_id, *, report=True, stop_terminal=True):
        events.append(("stop", account_id))
        worker.children.pop(account_id, None)

    def validate(check, *, keep_terminal=False):
        events.append(("validate", check["account_id"]))
        assert check["account_id"] not in worker.children
        return True, "ok"

    worker._stop_job = stop_job
    worker._validate_login = validate
    worker.sync_once(now=1)

    assert events == [("stop", 10), ("validate", 10)]
