from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def fake_mt5():
    class FakeMT5:
        POSITION_TYPE_BUY = 0
        SYMBOL_TRADE_MODE_FULL = 4
        ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
        TRADE_ACTION_DEAL = 1
        TRADE_ACTION_SLTP = 6
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_FILLING_FOK = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        SYMBOL_FILLING_FOK = 1
        SYMBOL_FILLING_IOC = 2
        SYMBOL_FILLING_RETURN = 4
        SYMBOL_TRADE_EXECUTION_MARKET = 2
        ORDER_TIME_GTC = 0
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008
        TRADE_RETCODE_REQUOTE = 10004
        TRADE_RETCODE_PRICE_CHANGED = 10020
        TRADE_RETCODE_TIMEOUT = 10012
        TRADE_RETCODE_CONNECTION = 10031
        TRADE_RETCODE_INVALID_FILL = 10030

        def __init__(self):
            self.positions = []
            self.sent = []
            self.next_ticket = 1200
            self.info = SimpleNamespace(volume_step=0.01, volume_min=0.01, volume_max=100.0,
                                        filling_mode=3, trade_exemode=2, point=0.01,
                                        digits=2, trade_stops_level=0, trade_freeze_level=0,
                                        visible=True)
            self.tick = SimpleNamespace(ask=100.0, bid=99.9)

        def positions_get(self, **kwargs):
            if "ticket" in kwargs:
                return tuple(p for p in self.positions if getattr(p, "ticket", None) == kwargs["ticket"])
            return tuple(self.positions)

        def symbol_info(self, symbol):
            return self.info

        def symbols_get(self):
            name = getattr(self.info, "name", None) or "XAUUSD"
            is_gold = name.upper().startswith("XAUUSD")
            return (SimpleNamespace(
                **{
                    **self.info.__dict__,
                    "name": name,
                    "visible": True,
                    "select": True,
                    "trade_mode": 4,
                    "currency_base": "XAU" if is_gold else "EUR",
                    "currency_profit": "USD",
                    "description": "Gold vs US Dollar" if is_gold else "Euro vs US Dollar",
                    "expiration_time": 0,
                }
            ),)

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return self.tick

        def account_info(self):
            return SimpleNamespace(margin_mode=2, balance=10000.0)

        def terminal_info(self):
            return SimpleNamespace(trade_allowed=True, connected=True)

        def order_send(self, request):
            self.sent.append(dict(request))
            action = request.get("action")
            ticket = int(request.get("position", 0) or 0)
            if action == self.TRADE_ACTION_DEAL and ticket:
                old = next((p for p in self.positions if p.ticket == ticket), None)
                if old is not None:
                    remaining = old.volume - request["volume"]
                    self.positions.remove(old)
                    if remaining > 1e-10:
                        self.positions.append(SimpleNamespace(**{**old.__dict__, "volume": remaining}))
            elif action == self.TRADE_ACTION_DEAL:
                self.next_ticket += 1
                ticket = self.next_ticket
                self.positions.append(SimpleNamespace(ticket=ticket, volume=request["volume"],
                                                       symbol=request["symbol"], type=request["type"],
                                                       magic=request.get("magic", 0),
                                                       price_current=request["price"], sl=request["sl"], tp=request["tp"]))
            elif action == self.TRADE_ACTION_SLTP:
                old = next((p for p in self.positions if p.ticket == ticket), None)
                if old is not None:
                    self.positions.remove(old)
                    self.positions.append(SimpleNamespace(**{**old.__dict__, "sl": request["sl"], "tp": request["tp"]}))
            return SimpleNamespace(retcode=10009, comment="done", order=ticket or 1234)

        def last_error(self):
            return (0, "ok")

        def initialize(self, **kwargs):
            return True

        def shutdown(self):
            return None

    return FakeMT5()


@pytest.fixture(autouse=True)
def install_metatrader5_module(fake_mt5, monkeypatch):
    """Expose the fake package to lazy imports without requiring a terminal."""
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
