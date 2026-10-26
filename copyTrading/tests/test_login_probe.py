import json
import shutil

from copytrade.login_probe import (
    _auth_failure_message,
    _probe,
    _read_status,
    _seed_broker_servers,
    _valid_access_point,
)
from copytrade import mt5_runtime
from copytrade.mt5_runtime import disable_terminal_mcp


def test_broker_access_point_requires_a_host_and_port():
    assert _valid_access_point("mt5.example.com:443")
    assert _valid_access_point("[2001:db8::1]:443")
    assert not _valid_access_point("Broker-Demo")
    assert not _valid_access_point("host:0")
    assert not _valid_access_point("user@host:443")


def test_native_probe_rejects_invalid_login_before_launching_terminal(tmp_path):
    valid, message = _probe({
        "path": str(tmp_path / "terminal64.exe"),
        "login": "0",
        "password": "private-password",
        "server": "Broker-Demo",
    })

    assert valid is False
    assert "angka positif" in message
    assert "private-password" not in message


def test_probe_reads_status_json_from_portable_terminal_files(tmp_path):
    status_path = tmp_path / "algentra-login-probe.json"
    status_path.write_text(json.dumps({
        "ready": True,
        "valid": True,
        "message": "Login broker berhasil.",
        "login": 12345,
        "server": "Broker-Demo",
    }), encoding="utf-8")

    status, failure = _read_status([status_path], {})

    assert failure is None
    assert status == {
        "ready": True,
        "valid": True,
        "message": "Login broker berhasil.",
        "login": 12345,
        "server": "Broker-Demo",
    }


def test_native_probe_explains_common_broker_rejections():
    assert "Invalid account" in _auth_failure_message("invalid account")
    assert "dinonaktifkan" in _auth_failure_message("account disabled")
    assert "Minta broker mengaktifkan izin trading" in _auth_failure_message("trading disabled on server")
    assert "No connection" not in _auth_failure_message("no connection")


def test_seed_broker_servers_prefers_master_terminal_and_keeps_existing_catalog(tmp_path):
    master = tmp_path / "master" / "terminal64.exe"
    master_catalog = master.parent / "Config" / "servers.dat"
    master_catalog.parent.mkdir(parents=True)
    master_catalog.write_bytes(b"master-broker-list")
    template = tmp_path / "template"
    template_catalog = template / "Config" / "servers.dat"
    template_catalog.parent.mkdir(parents=True)
    template_catalog.write_bytes(b"template-broker-list")
    account = tmp_path / "follower"

    _seed_broker_servers(account, template, master)

    target = account / "Config" / "servers.dat"
    assert target.read_bytes() == b"master-broker-list"
    target.write_bytes(b"existing-account-list")
    _seed_broker_servers(account, template, master)
    assert target.read_bytes() == b"existing-account-list"


def test_disable_terminal_mcp_preserves_other_settings_and_is_idempotent(tmp_path):
    config_dir = tmp_path / "Config"
    config_dir.mkdir()
    config = config_dir / "assistant.ini"
    config.write_text(
        "[MCP.MetaEditor]\nEnable=1\nEndpoint=http://127.0.0.1:22345/mcp\n"
        "\n[MCP.MetaTrader]\nEnable=1\nEndpoint=http://127.0.0.1:22346/mcp\nApiKey=keep-this-local\n",
        encoding="utf-8",
    )

    disable_terminal_mcp(tmp_path)
    first = config.read_text(encoding="utf-8")
    disable_terminal_mcp(tmp_path)
    second = config.read_text(encoding="utf-8")

    assert first == second
    assert "[MCP.MetaEditor]\nEnable=1" in second
    assert "[MCP.MetaTrader]\nEnable=0" in second
    assert "Endpoint=http://127.0.0.1:22346/mcp" in second
    assert "ApiKey=keep-this-local" in second


def test_remove_readonly_tree_retries_transient_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "prepayment-terminal"
    target.mkdir()
    (target / "locked-for-a-moment.dat").write_bytes(b"temporary")
    original_rmtree = shutil.rmtree
    attempts = 0

    def fail_once(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("file handle is closing")
        original_rmtree(path)

    monkeypatch.setattr(mt5_runtime.shutil, "rmtree", fail_once)
    monkeypatch.setattr(mt5_runtime.time, "sleep", lambda _seconds: None)

    mt5_runtime.remove_readonly_tree(target)

    assert attempts == 2
    assert not target.exists()
