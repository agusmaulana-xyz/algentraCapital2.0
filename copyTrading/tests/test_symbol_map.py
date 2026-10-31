from types import SimpleNamespace

import pytest

from copytrade.follower import FollowerEngine
from copytrade.symbol_map import SymbolResolver, map_symbol, resolve_symbol, select_symbol


class SymbolTerminal:
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self, symbols):
        self.symbols = {item.name: item for item in symbols}
        self.selected = []
        self.symbols_get_calls = 0
        self.symbol_info_calls = 0
        self.tick_calls = 0

    def symbols_get(self):
        self.symbols_get_calls += 1
        return tuple(self.symbols.values())

    def symbol_info(self, symbol):
        self.symbol_info_calls += 1
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol):
        self.tick_calls += 1
        item = self.symbols[symbol]
        return SimpleNamespace(bid=item.bid, ask=item.ask, time_msc=item.time_msc)

    def symbol_select(self, symbol, enabled):
        self.selected.append((symbol, enabled))
        if enabled and symbol in self.symbols:
            self.symbols[symbol].visible = True
            self.symbols[symbol].select = True
            return True
        return False

    def last_error(self):
        return (0, "ok")

    def initialize(self, **_kwargs):
        return True

    def account_info(self):
        return SimpleNamespace(
            margin_mode=2, currency="USD", balance=100_000.0,
            login=12345, server="Broker-Test", trade_allowed=True,
        )

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=True, path="")

    def positions_get(self, **_kwargs):
        return ()

    def shutdown(self):
        return None


def symbol(
    name,
    *,
    visible=False,
    selected=None,
    trade_mode=4,
    currency_base="XAU",
    currency_profit="USD",
    description="Gold vs US Dollar",
    expiration_time=0,
):
    return SimpleNamespace(
        name=name,
        visible=visible,
        select=visible if selected is None else selected,
        trade_mode=trade_mode,
        bid=2300.0,
        ask=2300.2,
        time_msc=100,
        currency_base=currency_base,
        currency_profit=currency_profit,
        description=description,
        expiration_time=expiration_time,
    )


@pytest.mark.parametrize(
    "broker_symbol",
    ["XAUUSD", "XAUUSDm", "XAUUSDc", "XAUUSDr", "XAUUSD.c", "XAUUSD+", "XAUUSD#", "XAU/USD"],
)
def test_resolves_common_broker_suffixes(broker_symbol):
    terminal = SymbolTerminal([symbol(broker_symbol)])

    assert resolve_symbol(terminal, "XAUUSD") == broker_symbol
    assert terminal.symbols_get_calls == 1
    assert terminal.symbol_info_calls == 0
    assert terminal.tick_calls == 0


def test_gold_alias_is_fallback_when_no_xauusd_prefix_is_available():
    terminal = SymbolTerminal([
        symbol("GOLD", currency_base="XAU", currency_profit="USD"),
        symbol("GOLDm", currency_base="XAU", currency_profit="USD"),
    ])

    assert resolve_symbol(terminal, "XAUUSD") == "GOLD"
    assert terminal.symbols_get_calls == 1


def test_prefixed_pair_wins_over_gold_alias():
    terminal = SymbolTerminal([
        symbol("GOLD", currency_base="XAU", currency_profit="USD"),
        symbol("XAUUSDm"),
    ])

    assert resolve_symbol(terminal, "XAUUSD") == "XAUUSDm"


def test_mixed_catalog_filters_crosses_and_derivatives_then_ranks_candidates():
    terminal = SymbolTerminal([
        symbol("XAUEUR", currency_base="XAU", currency_profit="EUR"),
        symbol("XAUUSDCAD", currency_base="XAU", currency_profit="CAD"),
        symbol("XAUUSD_o"),
        symbol("XAUUSD-FUTURE", description="Gold futures", expiration_time=1800000000),
        symbol("XAUUSDlong", visible=False),
        symbol("XAUUSD.c", visible=True),
    ])

    assert resolve_symbol(terminal, "XAUUSD") == "XAUUSD.c"
    assert terminal.symbols_get_calls == 1


def test_shortest_suffix_wins_after_visibility_and_trade_mode():
    terminal = SymbolTerminal([
        symbol("XAUUSDmicro", visible=False),
        symbol("XAUUSD", visible=False),
    ])

    assert resolve_symbol(terminal, "XAUUSD") == "XAUUSD"


def test_market_watch_selection_breaks_equal_suffix_tie():
    terminal = SymbolTerminal([
        symbol("XAUUSDm", visible=False, selected=False),
        symbol("XAUUSDc", visible=False, selected=True),
    ])

    assert resolve_symbol(terminal, "XAUUSD") == "XAUUSDc"


def test_visible_fully_tradable_symbol_has_first_priority():
    terminal = SymbolTerminal([
        symbol("XAUUSD", visible=False, trade_mode=4),
        symbol("XAUUSDm", visible=True, trade_mode=4),
        symbol("XAUUSDc", visible=True, trade_mode=0),
    ])

    assert resolve_symbol(terminal, "XAUUSD") == "XAUUSDm"


def test_non_gold_alias_without_xau_usd_metadata_is_rejected():
    terminal = SymbolTerminal([symbol("GOLD", currency_base="XAU", currency_profit="EUR")])

    with pytest.raises(ValueError, match="tidak tersedia atau tidak terverifikasi"):
        resolve_symbol(terminal, "XAUUSD")


def test_missing_gold_returns_warning_candidates_without_extra_queries():
    terminal = SymbolTerminal([symbol("XAUEUR", currency_base="XAU", currency_profit="EUR")])
    resolver = SymbolResolver(terminal, broker="Broker-X")

    assert resolver.resolve() is None
    assert terminal.symbols_get_calls == 1
    assert terminal.symbol_info_calls == 0
    assert terminal.tick_calls == 0


def test_symbol_selection_activates_only_the_winning_candidate():
    terminal = SymbolTerminal([symbol("XAUUSDc"), symbol("XAUUSDm")])

    assert select_symbol(terminal, "XAUUSD") == "XAUUSDc"
    assert terminal.selected == [("XAUUSDc", True)]
    assert terminal.symbols_get_calls == 1


def test_follower_login_succeeds_when_gold_symbol_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("copytrade.follower.start_portable_terminal", lambda _path: None)
    terminal = SymbolTerminal([])
    engine = FollowerEngine(
        {
            "nama": "account-without-gold",
            "terminal_path": "terminal64.exe",
            "login": 12345,
            "password_env": "FOLLOWER_PASSWORD",
            "server": "Broker-Test",
            "folder_sinyal": str(tmp_path),
            "portable": True,
            "map_simbol": {"XAUUSD": "XAUUSDm"},
            "mode_lot": "rasio",
            "rasio_lot": 1,
            "max_lot_per_order": 1,
            "max_posisi_terbuka": 3,
            "max_lot_total": 2,
            "dry_run": True,
        },
        terminal,
        tmp_path / "state.json",
    )

    engine.connect("secret-not-logged")

    assert engine.account_info.login == 12345
    assert engine.symbol_resolver.resolved is None
    assert engine.last_copy_error.startswith("Simbol emas XAUUSD belum tersedia")
    assert engine.config["map_simbol"]["XAUUSD"] == "XAUUSD"


def test_follower_connect_caches_automatic_broker_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr("copytrade.follower.start_portable_terminal", lambda _path: None)
    terminal = SymbolTerminal([symbol("XAUUSDm"), symbol("XAUUSDc")])
    engine = FollowerEngine(
        {
            "nama": "standard-account",
            "terminal_path": "terminal64.exe",
            "login": 12345,
            "password_env": "FOLLOWER_PASSWORD",
            "server": "Broker-Test",
            "folder_sinyal": str(tmp_path),
            "portable": True,
            "map_simbol": {"XAUUSD": "XAUUSD"},
            "mode_lot": "rasio",
            "rasio_lot": 1,
            "max_lot_per_order": 1,
            "max_posisi_terbuka": 3,
            "max_lot_total": 2,
            "dry_run": True,
        },
        terminal,
        tmp_path / "state.json",
    )

    engine.connect("secret-not-logged")
    calls_after_login = terminal.symbols_get_calls

    assert engine.symbol_resolver.resolved == "XAUUSDc"
    assert engine.config["map_simbol"]["XAUUSD"] == "XAUUSDc"
    assert engine.symbol_resolver.resolve("XAUUSD") == "XAUUSDc"
    assert terminal.symbols_get_calls == calls_after_login == 1


def test_gold_source_is_canonicalized_before_follower_local_resolution():
    config = {"map_simbol": {"XAUUSD": "XAUUSDm"}}

    assert map_symbol("XAUUSDc", config) == "XAUUSD"
    assert map_symbol("GOLD", config) == "XAUUSD"
