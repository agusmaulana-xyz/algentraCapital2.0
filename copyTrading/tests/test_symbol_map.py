from types import SimpleNamespace

import pytest

from copytrade.follower import FollowerEngine
from copytrade.symbol_map import resolve_symbol, select_symbol


class SymbolTerminal:
    SYMBOL_TRADE_MODE_DISABLED = 0

    def __init__(self, symbols):
        self.symbols = {item.name: item for item in symbols}
        self.selected = []

    def symbols_get(self, group=""):
        group = group.replace("*", "").upper()
        if group == "GOLD":
            return tuple(item for item in self.symbols.values() if "GOLD" in item.name.upper())
        return tuple(item for item in self.symbols.values() if item.name.upper().startswith(group))

    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol):
        item = self.symbols[symbol]
        return SimpleNamespace(bid=item.bid, ask=item.ask, time_msc=item.time_msc)

    def symbol_select(self, symbol, enabled):
        self.selected.append((symbol, enabled))
        return True

    def last_error(self):
        return (0, "ok")

    def initialize(self, **_kwargs):
        return True

    def account_info(self):
        return SimpleNamespace(margin_mode=2, currency="USC", balance=100_000.0)

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=True)

    def positions_get(self, **_kwargs):
        return ()

    def shutdown(self):
        return None


def symbol(name, *, bid=2300.0, ask=2300.2, time_msc=100, trade_mode=4):
    return SimpleNamespace(
        name=name,
        visible=False,
        trade_mode=trade_mode,
        bid=bid,
        ask=ask,
        time_msc=time_msc,
        currency_base="XAU",
        currency_profit="USD",
    )


def test_cent_account_uses_only_xauusdc_when_other_variants_exist():
    terminal = SymbolTerminal([symbol("XAUUSD"), symbol("XAUUSDm"), symbol("XAUUSDc")])

    assert resolve_symbol(terminal, "XAUUSD", SimpleNamespace(currency="USC")) == "XAUUSDc"


def test_cent_account_does_not_fall_back_when_xauusdc_is_missing():
    terminal = SymbolTerminal([symbol("XAUUSD"), symbol("XAUUSDm")])

    with pytest.raises(ValueError, match="hanya memakai XAUUSDc"):
        resolve_symbol(terminal, "XAUUSD", SimpleNamespace(currency="USC"))


def test_cent_account_normalizes_saved_gold_alias_to_xauusdc():
    terminal = SymbolTerminal([symbol("XAUUSD"), symbol("XAUUSDc"), symbol("XAUUSDm")])

    assert resolve_symbol(terminal, "XAUUSDm", SimpleNamespace(currency="USC")) == "XAUUSDc"


def test_regular_account_prefers_exact_xauusd_when_all_variants_are_available():
    terminal = SymbolTerminal([symbol("XAUUSD"), symbol("XAUUSDc"), symbol("XAUUSDm")])

    assert resolve_symbol(terminal, "XAUUSD", SimpleNamespace(currency="USD")) == "XAUUSD"


def test_missing_exact_symbol_falls_back_to_the_only_broker_variant():
    terminal = SymbolTerminal([symbol("XAUUSDm")])

    assert resolve_symbol(terminal, "XAUUSD", SimpleNamespace(currency="USD")) == "XAUUSDm"


def test_explicit_client_symbol_is_preserved_even_on_cent_account():
    terminal = SymbolTerminal([symbol("XAUUSDc"), symbol("XAUUSDm")])

    assert resolve_symbol(terminal, "XAUUSDc", SimpleNamespace(currency="USC")) == "XAUUSDc"


def test_select_symbol_enables_the_resolved_broker_symbol():
    terminal = SymbolTerminal([symbol("XAUUSDc")])

    resolved = select_symbol(terminal, "XAUUSD", SimpleNamespace(currency="USC"))

    assert resolved == "XAUUSDc"
    assert terminal.selected == [("XAUUSDc", True)]


def test_select_symbol_adds_symbol_before_reading_info_when_hidden():
    class HiddenUntilSelectedTerminal(SymbolTerminal):
        def symbol_info(self, name):
            if not any(symbol == name and selected for symbol, selected in self.selected):
                return None
            return super().symbol_info(name)

        def symbol_select(self, name, enabled):
            selected = super().symbol_select(name, enabled)
            if selected and enabled:
                self.symbols[name].visible = True
            return selected

    terminal = HiddenUntilSelectedTerminal([symbol("XAUUSDc")])

    resolved = select_symbol(terminal, "XAUUSD", SimpleNamespace(currency="USC"))

    assert resolved == "XAUUSDc"
    assert terminal.selected == [("XAUUSDc", True)]


def test_follower_connect_rewrites_default_mapping_to_cent_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr("copytrade.follower.start_portable_terminal", lambda _path: None)
    terminal = SymbolTerminal([symbol("XAUUSD"), symbol("XAUUSDc")])
    engine = FollowerEngine(
        {
            "nama": "cent-account",
            "terminal_path": "terminal64.exe",
            "login": 12345,
            "password_env": "FOLLOWER_PASSWORD",
            "server": "Broker-Cent",
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

    assert engine.config["map_simbol"]["XAUUSD"] == "XAUUSDc"
