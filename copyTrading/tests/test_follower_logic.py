from datetime import date
from types import SimpleNamespace
import json
import time

from copytrade.follower import FollowerEngine, _load_mt5, validate_config
from copytrade.protocol import Signal, encode_signal
from copytrade.signal_io import signal_path


def config(tmp_path, **extra):
    return {"nama": "demo", "terminal_path": "terminal.exe", "login": 1,
            "password_env": "PASS", "server": "Demo", "folder_sinyal": str(tmp_path / "signals"),
            "map_simbol": {}, "mode_lot": "rasio", "rasio_lot": 1.0, "max_lot_per_order": 5,
            "max_umur_sinyal_buka_detik": 3, "dry_run": True, "max_posisi_terbuka": 10,
            "max_lot_total": 10, **extra}


def open_signal(signal_id="open", ts=100, pos_id=10):
    return Signal(1, signal_id, ts, "buka", pos_id, "EURUSD", "buy", 0.1, 1.1, 1.0, 1.2, "")


def test_conftest_installs_meta_trader5_package_mock(fake_mt5):
    assert _load_mt5() is fake_mt5


def test_follower_config_rejects_false_string_for_live_switch(tmp_path):
    bad_config = config(tmp_path, dry_run="false")
    try:
        validate_config(bad_config)
    except ValueError as exc:
        assert "dry_run" in str(exc)
    else:
        raise AssertionError("string dry_run value must not be treated as a safe boolean")


def test_expired_and_stale_actions_and_idempotent_restart(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    assert engine.process_signal(open_signal(ts=10), now=20) == "lewati_kedaluwarsa"
    assert engine.positions == {}
    assert engine.process_signal(open_signal(signal_id="fresh", ts=100), now=100) == "sukses_dry_run"
    # All management actions are deliberately much older than the entry limit.
    assert engine.process_signal(Signal(1, "change", 1, "ubah", 10, sl=1.02, tp=1.2), now=100) == "sukses_dry_run"
    assert engine.process_signal(Signal(1, "partial", 1, "kurang", 10, lot=0.05, lot_sisa=0.05), now=100) == "sukses_dry_run"
    assert engine.positions["10"]["volume_current"] == 0.05
    assert engine.process_signal(Signal(1, "close", 1, "tutup", 10), now=100) == "sukses_dry_run"
    assert "10" not in engine.positions
    restored = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    assert restored.process_signal(open_signal(ts=100), now=100) == "sudah_diproses"


def test_state_and_idempotency_after_open(tmp_path, fake_mt5):
    path = tmp_path / "state.json"
    engine = FollowerEngine(config(tmp_path), fake_mt5, path)
    assert engine.process_signal(open_signal(), now=100) == "sukses_dry_run"
    assert engine.process_signal(open_signal(), now=100) == "sudah_diproses"
    restored = FollowerEngine(config(tmp_path), fake_mt5, path)
    assert restored.positions["10"]["ticket"] == "dry-10"
    assert restored.process_signal(open_signal(), now=100) == "sudah_diproses"


def test_missing_mapping_management_signal_is_safe(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    assert engine.process_signal(Signal(1, "close", 1, "tutup", 999)) == "lewati_tidak_terpetakan"


def test_mock_terminal_open_modify_partial_close_and_close(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path, dry_run=False), fake_mt5, tmp_path / "state.json")
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "sukses"
    ticket = engine.positions["10"]["ticket"]
    assert fake_mt5.positions[0].ticket == ticket
    assert engine.process_signal(Signal(1, "modify", 1, "ubah", 10, sl=1.02, tp=1.2)) == "sukses"
    assert fake_mt5.positions[0].sl == 1.02
    assert engine.process_signal(Signal(1, "partial-live", 1, "kurang", 10, lot=0.05, lot_sisa=0.05)) == "sukses"
    assert fake_mt5.positions[0].volume == 0.05
    assert engine.process_signal(Signal(1, "close-live", 1, "tutup", 10)) == "sukses"
    assert fake_mt5.positions == []
    assert engine.positions == {}


def test_initial_cursor_skips_existing_file_and_reads_appended_line(tmp_path, fake_mt5):
    today = date(2026, 10, 10)
    folder = tmp_path / "signals"
    path = signal_path(folder, today)
    path.parent.mkdir(parents=True)
    old = open_signal("old", ts=time.time())
    path.write_text(encode_signal(old) + "\n", encoding="utf-8")
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    assert engine.consume_once(today=today) == 0
    assert engine.positions == {}
    new = open_signal("new", ts=time.time(), pos_id=11)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(encode_signal(new) + "\n")
    assert engine.consume_once(today=today) == 1
    assert set(engine.positions) == {"11"}


def test_old_day_is_finished_before_rollover(tmp_path, fake_mt5):
    old_day, today = date(2026, 10, 9), date(2026, 10, 10)
    folder = tmp_path / "signals"
    old_path = signal_path(folder, old_day)
    old_path.parent.mkdir(parents=True)
    opened = open_signal("old-open", ts=time.time())
    close_line = encode_signal(Signal(1, "old-close", 1, "tutup", 10))
    prefix, suffix = close_line[:35], close_line[35:]
    with old_path.open("wb") as stream:
        stream.write((encode_signal(opened) + "\n" + prefix).encode())
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    engine.state["signal_date"] = old_day.isoformat()
    engine.state["offset"] = 0
    engine._persist()
    assert engine.consume_once(today=today) == 1
    assert engine.state["signal_date"] == old_day.isoformat()
    assert "10" in engine.positions
    with old_path.open("ab") as stream:
        stream.write((suffix + "\n").encode())
    assert engine.consume_once(today=today) == 1
    assert engine.state["signal_date"] == today.isoformat()
    assert engine.positions == {}


def test_retries_only_temporary_trade_retcode(tmp_path, fake_mt5, monkeypatch):
    engine = FollowerEngine(config(tmp_path, dry_run=False), fake_mt5, tmp_path / "state.json")
    original = fake_mt5.order_send
    attempts = []

    def requote_once(request):
        attempts.append(dict(request))
        if len(attempts) == 1:
            return SimpleNamespace(retcode=fake_mt5.TRADE_RETCODE_REQUOTE, comment="requote", order=0)
        return original(request)

    fake_mt5.order_send = requote_once
    monkeypatch.setattr("copytrade.follower.time.sleep", lambda _seconds: None)
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "sukses"
    assert len(attempts) == 2


def test_invalid_volume_is_not_retried(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path, dry_run=False), fake_mt5, tmp_path / "state.json")
    attempts = []

    def invalid_volume(request):
        attempts.append(dict(request))
        return SimpleNamespace(retcode=10014, comment="invalid volume", order=0)

    fake_mt5.order_send = invalid_volume
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "gagal_order"
    assert len(attempts) == 1


def test_cent_gold_uses_minimum_fallback_when_calculated_lot_is_below_broker_min(tmp_path, fake_mt5):
    fake_mt5.info.volume_min = 0.01
    fake_mt5.info.name = "XAUUSDc"
    fake_mt5.account_info = lambda: SimpleNamespace(
        margin_mode=2, balance=100.0, currency="USC", server="Broker-Cent",
    )
    engine = FollowerEngine(
        config(tmp_path, map_simbol={"XAUUSD": "XAUUSDc"}),
        fake_mt5,
        tmp_path / "state.json",
    )
    signal = Signal(1, "cent-small", time.time(), "buka", 10, "XAUUSD", "buy", 0.001, 2300, 0, 2301, "")

    assert engine.process_signal(signal, now=time.time()) == "sukses_dry_run"
    assert engine.positions["10"]["volume_initial"] == 0.01


def test_regular_symbol_uses_minimum_fallback_when_calculated_lot_is_below_broker_min(tmp_path, fake_mt5):
    fake_mt5.info.volume_min = 0.01
    fake_mt5.info.name = "EURUSD"
    fake_mt5.account_info = lambda: SimpleNamespace(
        margin_mode=2, balance=100.0, currency="USD", server="Broker-Standard",
    )
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    signal = Signal(1, "regular-small", time.time(), "buka", 10, "EURUSD", "buy", 0.001, 1.1, 0, 1.2, "")

    assert engine.process_signal(signal, now=time.time()) == "sukses_dry_run"
    assert engine.positions["10"]["volume_initial"] == 0.01


def test_zero_balance_mode_uses_minimum_broker_lot_when_enabled(tmp_path, fake_mt5):
    fake_mt5.info.volume_min = 0.01
    fake_mt5.info.name = "EURUSD"
    fake_mt5.account_info = lambda: SimpleNamespace(
        margin_mode=2, balance=0.0, currency="USD", server="Broker-Standard",
    )
    engine = FollowerEngine(
        config(tmp_path, mode_lot="saldo", saldo_master=1000.0),
        fake_mt5,
        tmp_path / "state.json",
    )
    signal = Signal(1, "zero-balance", time.time(), "buka", 10, "EURUSD", "buy", 0.1, 1.1, 0, 1.2, "")

    assert engine.process_signal(signal, now=time.time()) == "sukses_dry_run"
    assert engine.positions["10"]["volume_initial"] == 0.01


def test_cent_gold_retries_no_money_once_with_minimum_lot(tmp_path, fake_mt5):
    fake_mt5.info.name = "XAUUSDc"
    fake_mt5.account_info = lambda: SimpleNamespace(
        margin_mode=2, balance=100.0, currency="USC", server="Broker-Cent",
    )
    engine = FollowerEngine(
        config(tmp_path, dry_run=False, map_simbol={"XAUUSD": "XAUUSDc"}),
        fake_mt5,
        tmp_path / "state.json",
    )
    original = fake_mt5.order_send
    attempts = []

    def insufficient_then_success(request):
        attempts.append(dict(request))
        if len(attempts) == 1:
            return SimpleNamespace(retcode=10019, comment="No money", order=0)
        return original(request)

    fake_mt5.order_send = insufficient_then_success
    signal = Signal(1, "cent-margin", time.time(), "buka", 10, "XAUUSD", "buy", 0.1, 2300, 0, 2301, "")

    assert engine.process_signal(signal, now=time.time()) == "sukses"
    assert [request["volume"] for request in attempts] == [0.1, 0.01]
    assert fake_mt5.positions[0].volume == 0.01


def test_regular_gold_retries_no_money_once_at_broker_minimum(tmp_path, fake_mt5):
    fake_mt5.info.name = "XAUUSD"
    fake_mt5.account_info = lambda: SimpleNamespace(
        margin_mode=2, balance=100.0, currency="USD", server="Broker-Standard",
    )
    engine = FollowerEngine(
        config(tmp_path, dry_run=False, map_simbol={"XAUUSD": "XAUUSD"}),
        fake_mt5,
        tmp_path / "state.json",
    )
    attempts = []

    original = fake_mt5.order_send

    def insufficient_then_success(request):
        attempts.append(dict(request))
        if len(attempts) == 1:
            return SimpleNamespace(retcode=10019, comment="No money", order=0)
        return original(request)

    fake_mt5.order_send = insufficient_then_success
    signal = Signal(1, "regular-margin", time.time(), "buka", 10, "XAUUSD", "buy", 0.1, 2300, 0, 2301, "")

    assert engine.process_signal(signal, now=time.time()) == "sukses"
    assert [request["volume"] for request in attempts] == [0.1, 0.01]
    assert fake_mt5.positions[0].volume == 0.01


def test_retryable_retcode_allows_three_retries(tmp_path, fake_mt5, monkeypatch):
    engine = FollowerEngine(config(tmp_path, dry_run=False), fake_mt5, tmp_path / "state.json")
    attempts = []

    def price_changed(request):
        attempts.append(dict(request))
        return SimpleNamespace(retcode=fake_mt5.TRADE_RETCODE_PRICE_CHANGED, comment="changed", order=0)

    fake_mt5.order_send = price_changed
    monkeypatch.setattr("copytrade.follower.time.sleep", lambda _seconds: None)
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "gagal_order"
    assert len(attempts) == 4  # initial request plus at most three retries


def test_invalid_filling_advances_to_next_supported_type(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path, dry_run=False), fake_mt5, tmp_path / "state.json")
    original = fake_mt5.order_send
    attempts = []

    def invalid_fill_once(request):
        attempts.append(dict(request))
        if len(attempts) == 1:
            return SimpleNamespace(retcode=fake_mt5.TRADE_RETCODE_INVALID_FILL, comment="unsupported", order=0)
        return original(request)

    fake_mt5.order_send = invalid_fill_once
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "sukses"
    assert [request["type_filling"] for request in attempts] == [
        fake_mt5.ORDER_FILLING_FOK,
        fake_mt5.ORDER_FILLING_IOC,
    ]


def test_stop_levels_are_adjusted_to_minimum_distance(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=20, trade_freeze_level=10)
    assert engine._adjust_stops(info, "buy", 100, 99.95, 100.05) == (99.8, 100.2)


def test_zero_position_cap_means_no_application_limit(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path, max_posisi_terbuka=0), fake_mt5, tmp_path / "state.json")
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "sukses_dry_run"


def test_disconnected_terminal_is_a_retryable_connection_failure(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    fake_mt5.terminal_info = lambda: SimpleNamespace(connected=False, trade_allowed=False)
    try:
        engine.connect("secret")
    except ConnectionError as exc:
        assert "belum tersambung" in str(exc)
    else:
        raise AssertionError("disconnected terminal should be retried as a connection failure")


def test_account_history_report_exports_only_new_supported_trade_deals(tmp_path, fake_mt5):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    cursor_msc = int(datetime.now(timezone.utc).timestamp() * 1000) - 5000
    cursor_path = tmp_path / "report.cursor.json"
    cursor_path.write_text(json.dumps({"history_cursor": "20", "history_cursor_msc": cursor_msc, "scan_msc": cursor_msc}), encoding="utf-8")
    fake_mt5.DEAL_TYPE_BUY = 0
    fake_mt5.DEAL_TYPE_SELL = 1
    fake_mt5.DEAL_ENTRY_IN = 0
    fake_mt5.DEAL_ENTRY_OUT = 1
    fake_mt5.DEAL_ENTRY_INOUT = 2
    fake_mt5.DEAL_ENTRY_OUT_BY = 3
    fake_mt5.history_deals_get = lambda *_args: [
        SimpleNamespace(ticket=19, time_msc=cursor_msc - 1, position_id=7, type=0, entry=0, symbol="XAUUSD", volume=0.01, price=2300, profit=0, commission=0, swap=0, fee=0),
        SimpleNamespace(ticket=21, time_msc=cursor_msc + 1, position_id=7, type=0, entry=0, symbol="XAUUSD", volume=0.01, price=2300, profit=0, commission=0, swap=0, fee=0),
        SimpleNamespace(ticket=22, time_msc=cursor_msc + 2, position_id=7, type=1, entry=1, symbol="XAUUSD", volume=0.01, price=2302, profit=2, commission=-0.1, swap=0, fee=0),
        SimpleNamespace(ticket=23, time_msc=cursor_msc + 3, position_id=0, type=2, entry=0, symbol="", volume=0, price=0, profit=50, commission=0, swap=0, fee=0),
    ]
    engine = FollowerEngine(config(tmp_path), fake_mt5, tmp_path / "state.json")
    engine.config["report_cursor_path"] = str(cursor_path)

    report = engine._recent_history_deals()

    assert [deal["deal_ticket"] for deal in report] == ["21", "22"]
    assert report[0]["entry"] == "IN"
    assert report[1]["entry"] == "OUT"
    assert report[1]["action"] == "SELL"
    assert engine.report_scan_msc > cursor_msc


def test_none_positions_query_does_not_erase_follower_mapping(tmp_path, fake_mt5):
    engine = FollowerEngine(config(tmp_path, dry_run=False), fake_mt5, tmp_path / "state.json")
    assert engine.process_signal(open_signal(ts=time.time()), now=time.time()) == "sukses"
    fake_mt5.positions_get = lambda **_kwargs: None
    assert engine.process_signal(Signal(1, "modify-none", 1, "ubah", 10, sl=1.02, tp=1.2)) == "gagal_positions_get"
    assert engine.process_signal(Signal(1, "close-none", 1, "tutup", 10)) == "gagal_tutup"
    assert "10" in engine.positions
