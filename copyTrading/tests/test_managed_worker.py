import json
import sys
from types import SimpleNamespace

import pytest

from copytrade.managed_worker import ManagedWorker, _resolve, enable_terminal_auto_trading, load_config


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


def test_validate_login_returns_safe_success_and_shuts_down(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "terminal64.exe").write_bytes(b"test-terminal")
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.terminals_dir = tmp_path / "runtime" / "terminals"
    worker.template_dir = template
    fake_mt5 = SimpleNamespace(
        initialize=lambda **kwargs: kwargs["password"] == "private-password",
        account_info=lambda: SimpleNamespace(login=12345, trade_allowed=True),
        terminal_info=lambda: SimpleNamespace(connected=True, trade_allowed=True),
        shutdown=lambda: None,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    valid, message = worker._validate_login({
        "account_id": 8,
        "login": "12345",
        "server": "Broker-Demo",
        "broker_password": "private-password",
    })

    assert valid is True
    assert message == "Login broker berhasil dan terminal siap untuk copy trading."
    ini = (worker.terminals_dir / "8" / "Config" / "common.ini").read_text(encoding="utf-8")
    assert "AllowLiveTrading=1" in ini
