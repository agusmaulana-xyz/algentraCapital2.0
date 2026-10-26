from types import SimpleNamespace

from copytrade.master_publisher import SignalIdGenerator, diff_snapshot, reconnect_master, snapshot_positions


def position(identifier, *, volume=0.1, sl=0, tp=0, comment="", magic=1):
    return SimpleNamespace(identifier=identifier, ticket=identifier + 100, symbol="EURUSD", type=0,
                           volume=volume, price_open=1.1, sl=sl, tp=tp, comment=comment, magic=magic)


def test_diff_new_change_partial_close_and_full_close():
    baseline = snapshot_positions([position(1), position(2)])
    result = diff_snapshot(baseline, [position(1, volume=0.05, sl=1.0), position(3)])
    events, current = result
    assert [e["aksi"] for e in events] == ["ubah", "kurang", "buka", "tutup"]
    assert events[1]["lot"] == 0.05
    assert events[1]["lot_sisa"] == 0.05
    assert set(current) == {1, 3}


def test_none_does_not_become_empty_snapshot():
    baseline = snapshot_positions([position(1)])
    assert diff_snapshot(baseline, None) is None


def test_filters_apply_to_baseline_and_updates():
    current = snapshot_positions([position(1, magic=2), position(2, comment="ignore this")],
                                 {"filter_magic": [1], "abaikan_komentar_mengandung": ["ignore"]})
    assert current == {}


def test_signal_ids_are_unique_and_monotonically_increasing(monkeypatch):
    clock = [1760000000.001]
    monkeypatch.setattr("copytrade.master_publisher.time.time", lambda: clock[0])
    ids = SignalIdGenerator()
    first, second = ids.next(), ids.next()
    clock[0] -= 5
    third = ids.next()
    assert first == "1760000000001-0"
    assert second == "1760000000001-1"
    assert third == "1760000000001-2"


def test_reconnect_master_reinitializes_only_the_configured_source_terminal():
    class FakeMT5:
        def __init__(self):
            self.shutdown_calls = 0
            self.initialize_calls = []

        def shutdown(self):
            self.shutdown_calls += 1

        def initialize(self, **options):
            self.initialize_calls.append(options)
            return True

    mt5 = FakeMT5()

    assert reconnect_master(mt5, r"C:\\Master\\terminal64.exe") is True
    assert mt5.shutdown_calls == 1
    assert mt5.initialize_calls == [{"path": r"C:\\Master\\terminal64.exe", "timeout": 10_000}]
