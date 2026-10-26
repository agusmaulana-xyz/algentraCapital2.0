"""One MT5 follower process. Keep one instance per terminal/account."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping

from .logging_setup import configure_logging
from .lot_calc import calculate_lot
from .protocol import Signal, parse_signal
from .signal_io import read_new, signal_path
from .state import load_state, save_state
from .symbol_map import map_symbol, select_symbol

logger = logging.getLogger("follower")

RETRYABLE_RETCODES = ("TRADE_RETCODE_REQUOTE", "TRADE_RETCODE_PRICE_CHANGED", "TRADE_RETCODE_TIMEOUT", "TRADE_RETCODE_CONNECTION")


def _load_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Package MetaTrader5 hanya tersedia di Windows; jalankan pada VPS Windows") from exc
    return mt5


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _date_value(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


class FollowerEngine:
    def __init__(self, config: Mapping[str, Any], mt5: Any, state_path: str | Path, *, logger_: logging.Logger | None = None):
        self.config = dict(config)
        self.mt5 = mt5
        self.state_path = Path(state_path)
        self.logger = logger_ or logger
        self.account_info: Any = None
        self.state = load_state(state_path)
        self.processed = set(str(item) for item in self.state.get("processed", []))
        self.dry_positions: dict[str, dict[str, Any]] = {}
        self.last_reconcile = 0.0
        self.last_heartbeat = 0.0
        self.last_account_report = 0.0
        self.report_scan_msc = 0
        self.heartbeat_path = Path(self.config.get("heartbeat_folder", ".")) / f"heartbeat_{self.config['nama']}.txt"

    @property
    def positions(self) -> dict[str, dict[str, Any]]:
        return self.state["positions"]

    def _persist(self) -> None:
        self.state["processed"] = list(self.processed)
        save_state(self.state_path, self.state)

    def connect(self, password: str) -> None:
        initialize_options = dict(
            path=self.config["terminal_path"],
            login=int(self.config["login"]),
            password=password,
            server=self.config["server"],
        )
        if self.config.get("portable", False):
            initialize_options["portable"] = True
        if not self.mt5.initialize(**initialize_options):
            raise ConnectionError(f"mt5.initialize gagal: {self.mt5.last_error()}")
        account = self.mt5.account_info()
        if account is None:
            raise ConnectionError(f"account_info gagal: {self.mt5.last_error()}")
        self.account_info = account
        hedging = getattr(self.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        if _get(account, "margin_mode") != hedging:
            self.logger.error("PERINGATAN KERAS: akun netting; pemetaan satu posisi ke satu ticket tidak akurat")
            if not self.config.get("izinkan_netting", False):
                self.mt5.shutdown()
                raise RuntimeError("Akun netting ditolak; set izinkan_netting=true hanya jika menerima keterbatasannya")
        terminal = self.mt5.terminal_info()
        if terminal is None or not _get(terminal, "connected", False):
            self.mt5.shutdown()
            raise ConnectionError(f"Terminal follower belum tersambung: {self.mt5.last_error()}")
        if not _get(terminal, "trade_allowed", False):
            self.mt5.shutdown()
            raise RuntimeError("Algo Trading belum menyala di terminal atau Allow algorithmic trading belum aktif")
        try:
            for master_symbol, requested in list(self.config.get("map_simbol", {}).items()):
                resolved = select_symbol(self.mt5, str(requested), account)
                self.config["map_simbol"][master_symbol] = resolved
                if resolved != str(requested):
                    self.logger.info(
                        "Simbol broker otomatis dipilih %s -> %s (akun %s)",
                        requested,
                        resolved,
                        str(_get(account, "currency", "")) or "cent/regular",
                    )
        except Exception:
            self.mt5.shutdown()
            raise
        self.logger.info("Terhubung ke akun follower %s", self.config["nama"])
        self.reconcile(force=True)

    def _market_price(self, symbol: str, side: str) -> float | None:
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return float(_get(tick, "ask" if side == "buy" else "bid", 0.0))

    def _fillings(self, symbol_info: Any) -> list[int]:
        supported = int(_get(symbol_info, "filling_mode", 0) or 0)
        candidates: list[int] = []
        for flag_name, order_name in (("SYMBOL_FILLING_FOK", "ORDER_FILLING_FOK"), ("SYMBOL_FILLING_IOC", "ORDER_FILLING_IOC")):
            flag = int(getattr(self.mt5, flag_name, 1 if flag_name.endswith("FOK") else 2))
            if supported & flag:
                candidates.append(int(getattr(self.mt5, order_name, 0 if order_name.endswith("FOK") else 1)))
        return_flag = int(getattr(self.mt5, "SYMBOL_FILLING_RETURN", 4))
        market_exec = int(getattr(self.mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2))
        if supported & return_flag or int(_get(symbol_info, "trade_exemode", -1)) != market_exec:
            candidates.append(int(getattr(self.mt5, "ORDER_FILLING_RETURN", 2)))
        # Some broker/test doubles expose only a single filling value. Do not invent
        # another supported mode when the symbol says none are available.
        return list(dict.fromkeys(candidates))

    def _send(self, request: dict[str, Any], symbol_info: Any) -> Any:
        retry_codes = {getattr(self.mt5, name, object()) for name in RETRYABLE_RETCODES}
        invalid_fill = getattr(self.mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        is_deal = request.get("action") == getattr(self.mt5, "TRADE_ACTION_DEAL", 1)
        candidates: list[int | None] = self._fillings(symbol_info) if is_deal else [None]
        if not candidates:
            self.logger.error("Tidak menemukan type_filling yang didukung simbol")
            return None
        for fill in candidates:
            if fill is not None:
                request["type_filling"] = fill
            for attempt in range(4):
                result = self.mt5.order_send(request)
                if result is None:
                    code = None
                    comment = str(self.mt5.last_error())
                else:
                    code = _get(result, "retcode")
                    comment = str(_get(result, "comment", ""))
                self.logger.info("order_send attempt=%d retcode=%s comment=%s", attempt + 1, code, comment)
                if result is not None and self._is_success(code):
                    return result
                if code == invalid_fill:
                    break
                if code in retry_codes and attempt < 3:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                return result
        return None

    def _is_success(self, retcode: Any) -> bool:
        return retcode in {
            getattr(self.mt5, "TRADE_RETCODE_DONE", 10009),
            getattr(self.mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010),
            getattr(self.mt5, "TRADE_RETCODE_PLACED", 10008),
        }

    def _adjust_stops(self, symbol_info: Any, side: str, price: float, sl: float | None, tp: float | None) -> tuple[float, float]:
        point = float(_get(symbol_info, "point", 0.0) or 0.0)
        distance = max(int(_get(symbol_info, "trade_stops_level", 0) or 0), int(_get(symbol_info, "trade_freeze_level", 0) or 0)) * point
        digits = int(_get(symbol_info, "digits", 5) or 5)
        adjusted_sl = float(sl or 0.0)
        adjusted_tp = float(tp or 0.0)
        if distance > 0:
            if side == "buy":
                if adjusted_sl > 0 and adjusted_sl > price - distance:
                    adjusted_sl = price - distance
                    self.logger.warning("SL disesuaikan ke jarak minimum %s", adjusted_sl)
                if adjusted_tp > 0 and adjusted_tp < price + distance:
                    adjusted_tp = price + distance
                    self.logger.warning("TP disesuaikan ke jarak minimum %s", adjusted_tp)
            else:
                if adjusted_sl > 0 and adjusted_sl < price + distance:
                    adjusted_sl = price + distance
                    self.logger.warning("SL disesuaikan ke jarak minimum %s", adjusted_sl)
                if adjusted_tp > 0 and adjusted_tp > price - distance:
                    adjusted_tp = price - distance
                    self.logger.warning("TP disesuaikan ke jarak minimum %s", adjusted_tp)
        return round(adjusted_sl, digits), round(adjusted_tp, digits)

    def _safety_check(self, volume: float) -> str | None:
        raw_positions = self.mt5.positions_get()
        if raw_positions is None:
            return f"positions_get gagal saat cek batas: {self.mt5.last_error()}"
        try:
            max_positions = int(self.config["max_posisi_terbuka"])
            max_lots = float(self.config["max_lot_total"])
        except (KeyError, TypeError, ValueError):
            return "batas max_posisi_terbuka dan max_lot_total wajib dikonfigurasi"
        if max_positions <= 0 or not math.isfinite(max_lots) or max_lots <= 0:
            return "batas max_posisi_terbuka dan max_lot_total harus lebih besar dari 0"
        open_count = len(raw_positions)
        total_lots = sum(float(_get(pos, "volume", 0.0) or 0.0) for pos in raw_positions)
        if self.config.get("dry_run", True):
            open_count += len(self.positions)
            total_lots += sum(float(mapping.get("volume_current", 0.0)) for mapping in self.positions.values())
        if open_count >= max_positions:
            return f"batas max_posisi_terbuka={max_positions} tercapai"
        if total_lots + volume > max_lots + 1e-10:
            return f"batas max_lot_total={max_lots:g} akan terlampaui"
        return None

    def process_signal(self, raw_signal: Signal | Mapping[str, Any] | str, *, now: float | None = None) -> str:
        signal = parse_signal(raw_signal)
        if signal.id in self.processed:
            return "sudah_diproses"
        try:
            if signal.aksi == "buka":
                result = self._open(signal, time.time() if now is None else now)
            elif signal.aksi == "ubah":
                result = self._modify(signal)
            elif signal.aksi == "kurang":
                result = self._partial_close(signal)
            else:
                result = self._close(signal)
        except Exception:
            self.logger.exception("Sinyal %s aksi=%s gagal", signal.id, signal.aksi)
            result = "gagal"
        self.processed.add(signal.id)
        self._persist()
        self.logger.info("Sinyal %s aksi=%s hasil=%s", signal.id, signal.aksi, result)
        return result

    def _open(self, signal: Signal, now: float) -> str:
        age = now - signal.ts
        if age > float(self.config.get("max_umur_sinyal_buka_detik", 3)):
            self.logger.info("sinyal buka kedaluwarsa id=%s umur=%.3fs", signal.id, age)
            return "lewati_kedaluwarsa"
        key = str(signal.pos_id)
        if key in self.positions:
            return "lewati_sudah_terpetakan"
        requested_symbol = map_symbol(str(signal.simbol), self.config)
        symbol = select_symbol(self.mt5, requested_symbol, self.account_info)
        if symbol != requested_symbol:
            self.config.setdefault("map_simbol", {})[str(signal.simbol)] = symbol
            self.logger.info("Simbol sinyal %s diarahkan otomatis ke %s", requested_symbol, symbol)
        info = self.mt5.symbol_info(symbol)
        account = self.mt5.account_info()
        lot = calculate_lot(
            float(signal.lot), info, self.config,
            balance_follower=_get(account, "balance") if account is not None else None,
            balance_master=self.config.get("saldo_master"),
        )
        if not lot.ok:
            self.logger.warning("Order follower dilewati: %s", lot.reason)
            return "lewati_lot"
        volume = float(lot.volume)
        safety_reason = self._safety_check(volume)
        if safety_reason:
            self.logger.warning("Order follower dilewati: %s", safety_reason)
            return "lewati_batas_pengaman"
        side = str(signal.arah)
        if self.config.get("arah_terbalik", False):
            side = "sell" if side == "buy" else "buy"
        price = self._market_price(symbol, side)
        if price is None or price <= 0:
            self.logger.error("Tidak mendapat harga pasar untuk %s", symbol)
            return "gagal_harga"
        sl, tp = self._adjust_stops(info, side, price, signal.sl, signal.tp)
        if self.config.get("dry_run", True):
            ticket: Any = f"dry-{signal.pos_id}"
            self.logger.info("DRY RUN order_send symbol=%s side=%s volume=%s price=%s sl=%s tp=%s", symbol, side, volume, price, sl, tp)
        else:
            order_type = getattr(self.mt5, "ORDER_TYPE_BUY", 0) if side == "buy" else getattr(self.mt5, "ORDER_TYPE_SELL", 1)
            request = {
                "action": getattr(self.mt5, "TRADE_ACTION_DEAL", 1), "symbol": symbol, "volume": volume,
                "type": order_type, "price": price, "sl": sl, "tp": tp,
                "deviation": int(self.config.get("deviation_poin", 20)), "magic": int(self.config.get("magic", 880001)),
                "comment": str(self.config.get("komentar", "copy"))[:31], "type_time": getattr(self.mt5, "ORDER_TIME_GTC", 0),
            }
            result = self._send(request, info)
            if result is None or not self._is_success(_get(result, "retcode")):
                self.logger.error("Buka gagal retcode=%s comment=%s", _get(result, "retcode"), _get(result, "comment", ""))
                return "gagal_order"
            ticket = int(_get(result, "order", 0) or 0)
            found = self.mt5.positions_get(ticket=ticket) if ticket else None
            if not found:
                self.logger.error("Order %s sukses tetapi posisi belum dapat diverifikasi; cek terminal manual", ticket)
                return "gagal_verifikasi_posisi"
            ticket = int(_get(found[0], "ticket", ticket))
        self.positions[key] = {
            "ticket": ticket,
            "volume_initial": volume,
            "volume_current": volume,
            "master_initial": float(signal.lot),
            "side": side,
            "symbol": symbol,
        }
        if self.config.get("dry_run", True):
            self.dry_positions[key] = dict(self.positions[key])
        return "sukses_dry_run" if self.config.get("dry_run", True) else "sukses"

    def _mapped_position(self, pos_id: int) -> tuple[str, dict[str, Any]] | None:
        key = str(pos_id)
        mapping = self.positions.get(key)
        if mapping is None:
            self.logger.info("Posisi master %s tidak terpetakan; aksi dilewati", pos_id)
            return None
        return key, mapping

    def _modify(self, signal: Signal) -> str:
        mapped = self._mapped_position(signal.pos_id)
        if mapped is None:
            return "lewati_tidak_terpetakan"
        key, mapping = mapped
        if self.config.get("dry_run", True):
            self.logger.info("DRY RUN ubah SL/TP ticket=%s", mapping["ticket"])
            return "sukses_dry_run"
        pos_rows = self.mt5.positions_get(ticket=int(mapping["ticket"]))
        if pos_rows is None:
            self.logger.error("positions_get gagal saat ubah ticket=%s: %s", mapping["ticket"], self.mt5.last_error())
            return "gagal_positions_get"
        if not pos_rows:
            self.positions.pop(key, None)
            return "lewati_posisi_sudah_tidak_ada"
        info = self.mt5.symbol_info(mapping["symbol"])
        side = mapping["side"]
        price = float(_get(pos_rows[0], "price_current", 0.0) or self._market_price(mapping["symbol"], side) or 0.0)
        sl, tp = self._adjust_stops(info, side, price, signal.sl, signal.tp)
        request = {"action": getattr(self.mt5, "TRADE_ACTION_SLTP", 6), "position": int(mapping["ticket"]),
                   "symbol": mapping["symbol"], "sl": sl, "tp": tp, "magic": int(self.config.get("magic", 880001))}
        result = self._send(request, info)
        return "sukses" if result is not None and self._is_success(_get(result, "retcode")) else "gagal_ubah"

    def _close_volume(self, key: str, mapping: dict[str, Any], volume: float) -> bool:
        if self.config.get("dry_run", True):
            mapping["volume_current"] = max(0.0, float(mapping["volume_current"]) - volume)
            if mapping["volume_current"] <= 1e-10:
                self.positions.pop(key, None)
                self.dry_positions.pop(key, None)
            return True
        ticket = int(mapping["ticket"])
        rows = self.mt5.positions_get(ticket=ticket)
        if rows is None:
            self.logger.error("positions_get gagal saat tutup ticket=%s: %s", ticket, self.mt5.last_error())
            return False
        if not rows:
            self.positions.pop(key, None)
            return True
        position = rows[0]
        info = self.mt5.symbol_info(mapping["symbol"])
        close_side = "sell" if mapping["side"] == "buy" else "buy"
        price = self._market_price(mapping["symbol"], close_side)
        if price is None:
            return False
        request = {
            "action": getattr(self.mt5, "TRADE_ACTION_DEAL", 1), "symbol": mapping["symbol"], "position": ticket,
            "volume": volume, "type": getattr(self.mt5, "ORDER_TYPE_BUY", 0) if close_side == "buy" else getattr(self.mt5, "ORDER_TYPE_SELL", 1),
            "price": price, "deviation": int(self.config.get("deviation_poin", 20)),
            "magic": int(self.config.get("magic", 880001)), "comment": str(self.config.get("komentar", "copy"))[:31],
            "type_time": getattr(self.mt5, "ORDER_TIME_GTC", 0),
        }
        result = self._send(request, info)
        if result is None or not self._is_success(_get(result, "retcode")):
            self.logger.error("Tutup ticket=%s gagal retcode=%s comment=%s", ticket, _get(result, "retcode"), _get(result, "comment", ""))
            return False
        remaining = float(_get(position, "volume", mapping.get("volume_current", 0.0))) - volume
        if remaining <= 1e-10:
            self.positions.pop(key, None)
        else:
            mapping["volume_current"] = remaining
        return True

    def _partial_close(self, signal: Signal) -> str:
        mapped = self._mapped_position(signal.pos_id)
        if mapped is None:
            return "lewati_tidak_terpetakan"
        key, mapping = mapped
        current = float(mapping.get("volume_current", mapping["volume_initial"]))
        initial_follower = float(mapping["volume_initial"])
        master_initial = float(mapping["master_initial"])
        info = self.mt5.symbol_info(mapping["symbol"])
        minimum = float(_get(info, "volume_min", 0.01))
        step = float(_get(info, "volume_step", 0.01))
        target_decimal = Decimal(str(initial_follower)) * Decimal(str(signal.lot_sisa)) / Decimal(str(master_initial))
        if target_decimal < Decimal(str(minimum)):
            close_volume = current
        else:
            raw_close_decimal = max(Decimal("0"), Decimal(str(current)) - target_decimal)
            close_volume = math_floor_step(float(raw_close_decimal), step)
            if Decimal(str(close_volume)) < Decimal(str(minimum)):
                self.logger.info("Partial close terlalu kecil untuk minimum lot; posisi follower dipertahankan")
                return "lewati_di_bawah_minimum"
            if Decimal(str(current)) - Decimal(str(close_volume)) < Decimal(str(minimum)):
                close_volume = current
        if not self._close_volume(key, mapping, close_volume):
            return "gagal_partial_close"
        return "sukses_dry_run" if self.config.get("dry_run", True) else "sukses"

    def _close(self, signal: Signal) -> str:
        mapped = self._mapped_position(signal.pos_id)
        if mapped is None:
            return "lewati_tidak_terpetakan"
        key, mapping = mapped
        volume = float(mapping.get("volume_current", mapping["volume_initial"]))
        if not self._close_volume(key, mapping, volume):
            return "gagal_tutup"
        return "sukses_dry_run" if self.config.get("dry_run", True) else "sukses"

    def reconcile(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_reconcile < 30:
            return
        self.last_reconcile = now
        if self.config.get("dry_run", True):
            return
        rows = self.mt5.positions_get()
        if rows is None:
            self.logger.error("Rekonsiliasi gagal; positions_get: %s", self.mt5.last_error())
            return
        tickets = {int(_get(pos, "ticket", 0)) for pos in rows}
        mapped_tickets = {int(mapping["ticket"]) for mapping in self.positions.values()}
        follower_magic = int(self.config.get("magic", 880001))
        for position in rows:
            ticket = int(_get(position, "ticket", 0))
            if int(_get(position, "magic", 0) or 0) == follower_magic and ticket not in mapped_tickets:
                self.logger.info("Posisi follower ticket=%s dengan magic=%s tidak memiliki mapping; dibiarkan terbuka",
                                 ticket, follower_magic)
        removed = [key for key, mapping in self.positions.items() if int(mapping["ticket"]) not in tickets]
        for key in removed:
            self.logger.info("Posisi follower ticket=%s sudah tidak ada; mapping dihapus", self.positions[key]["ticket"])
            self.positions.pop(key, None)
        if removed:
            self._persist()

    def _bootstrap_cursor(self, today: date) -> None:
        if self.state.get("signal_date") is not None:
            return
        path = signal_path(self.config["folder_sinyal"], today)
        self.state["signal_date"] = today.isoformat()
        self.state["offset"] = path.stat().st_size if path.exists() else 0
        self.logger.info("State belum ada; mulai membaca dari akhir file sinyal hari ini (offset=%d)", self.state["offset"])
        self._persist()

    def consume_once(self, *, today: date | None = None) -> int:
        today = today or datetime.now().astimezone().date()
        self._bootstrap_cursor(today)
        active_date = _date_value(self.state["signal_date"])
        assert active_date is not None
        path = signal_path(self.config["folder_sinyal"], active_date)
        if active_date < today and not path.exists():
            self.state["signal_date"] = (active_date + timedelta(days=1)).isoformat()
            self.state["offset"] = 0
            self._persist()
            return 0
        result = read_new(path, int(self.state.get("offset", 0)))
        count = 0
        for record in result.records:
            self.process_signal(record.signal)
            self.state["offset"] = record.end_offset
            self._persist()
            count += 1
        if not result.records and result.offset != int(self.state.get("offset", 0)):
            self.state["offset"] = result.offset
            self._persist()
        if active_date < today and not result.has_partial_line and path.exists() and result.offset >= path.stat().st_size:
            self.state["signal_date"] = (active_date + timedelta(days=1)).isoformat()
            self.state["offset"] = 0
            self._persist()
        return count

    def heartbeat(self) -> None:
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.heartbeat_path.with_suffix(self.heartbeat_path.suffix + ".tmp")
        temporary.write_text(datetime.now().astimezone().isoformat(), encoding="utf-8")
        os.replace(temporary, self.heartbeat_path)

    def write_account_report(self) -> None:
        report_value = self.config.get("report_path")
        if not report_value:
            return
        account = self.mt5.account_info()
        terminal = self.mt5.terminal_info()
        positions = self.mt5.positions_get()
        if account is None or terminal is None or positions is None:
            self.logger.warning("Laporan akun dilewati karena data terminal belum lengkap")
            return
        raw_mode = _get(account, "trade_mode", None)
        if raw_mode == getattr(self.mt5, "ACCOUNT_TRADE_MODE_REAL", 2):
            trade_mode = "real"
        elif raw_mode == getattr(self.mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1):
            trade_mode = "contest"
        else:
            trade_mode = "demo"
        floating = sum(
            float(_get(position, "profit", 0.0) or 0.0) + float(_get(position, "swap", 0.0) or 0.0)
            for position in positions
        )
        deals = self._recent_history_deals()
        payload = {
            "balance": float(_get(account, "balance", 0.0) or 0.0),
            "equity": float(_get(account, "equity", 0.0) or 0.0),
            "floating_profit": floating,
            "margin": max(0.0, float(_get(account, "margin", 0.0) or 0.0)),
            "free_margin": float(_get(account, "margin_free", 0.0) or 0.0),
            "currency": str(_get(account, "currency", "USD") or "USD"),
            "trade_mode": trade_mode,
            "allow_live_trading": trade_mode == "real",
            "terminal_trade_allowed": bool(_get(terminal, "trade_allowed", False)),
            "expert_trade_allowed": bool(_get(terminal, "trade_expert", False)),
            "open_position_ids": [str(_get(position, "ticket", "")) for position in positions],
            "deals": deals,
            "history_scan_msc": self.report_scan_msc,
        }
        report_path = Path(report_value)
        try:
            serialized = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = report_path.with_suffix(report_path.suffix + ".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            os.replace(temporary, report_path)
        except (OSError, ValueError) as exc:
            self.logger.warning("Laporan akun tidak dapat disimpan (%s)", type(exc).__name__)

    def _recent_history_deals(self) -> list[dict[str, Any]]:
        cursor_path_value = self.config.get("report_cursor_path")
        cursor_ticket = 0
        cursor_msc = 0
        scan_msc = 0
        if cursor_path_value:
            try:
                cursor = json.loads(Path(cursor_path_value).read_text(encoding="utf-8"))
                cursor_ticket = max(0, int(cursor.get("history_cursor", 0)))
                cursor_msc = max(0, int(cursor.get("history_cursor_msc", 0)))
                scan_msc = max(0, int(cursor.get("scan_msc", 0)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        now = datetime.now(timezone.utc)
        start_msc = max(cursor_msc, scan_msc) or int((now - timedelta(days=30)).timestamp() * 1000)
        start = datetime.fromtimestamp(max(0, start_msc - 1000) / 1000, timezone.utc)
        try:
            rows = self.mt5.history_deals_get(start, now)
        except (AttributeError, TypeError):
            self.logger.warning("Riwayat deal tidak tersedia pada terminal ini")
            return []
        if rows is None:
            self.logger.warning("Riwayat deal belum dapat dibaca; akan dicoba lagi")
            return []
        buy = getattr(self.mt5, "DEAL_TYPE_BUY", 0)
        sell = getattr(self.mt5, "DEAL_TYPE_SELL", 1)
        entry_names = {
            getattr(self.mt5, "DEAL_ENTRY_IN", 0): "IN",
            getattr(self.mt5, "DEAL_ENTRY_OUT", 1): "OUT",
            getattr(self.mt5, "DEAL_ENTRY_INOUT", 2): "INOUT",
            getattr(self.mt5, "DEAL_ENTRY_OUT_BY", 3): "OUT_BY",
        }
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        for deal in rows:
            deal_type = _get(deal, "type")
            if deal_type not in (buy, sell):
                continue
            ticket = int(_get(deal, "ticket", 0) or 0)
            time_msc = int(_get(deal, "time_msc", 0) or 0)
            position_id = int(_get(deal, "position_id", 0) or 0)
            entry = entry_names.get(_get(deal, "entry"))
            symbol = str(_get(deal, "symbol", "") or "")
            if ticket <= 0 or time_msc <= 0 or position_id <= 0 or entry is None or not symbol:
                continue
            if cursor_msc and (time_msc, ticket) <= (cursor_msc, cursor_ticket):
                continue
            payload = {
                "deal_ticket": str(ticket),
                "position_id": str(position_id),
                "time_msc": time_msc,
                "symbol": symbol,
                "action": "BUY" if deal_type == buy else "SELL",
                "entry": entry,
                "volume": float(_get(deal, "volume", 0.0) or 0.0),
                "price": float(_get(deal, "price", 0.0) or 0.0),
                "profit": float(_get(deal, "profit", 0.0) or 0.0),
                "commission": float(_get(deal, "commission", 0.0) or 0.0),
                "swap": float(_get(deal, "swap", 0.0) or 0.0),
                "fee": float(_get(deal, "fee", 0.0) or 0.0),
            }
            candidates.append((time_msc, ticket, payload))
        candidates.sort(key=lambda item: (item[0], item[1]))
        if len(candidates) <= 100:
            self.report_scan_msc = int(now.timestamp() * 1000)
        else:
            self.report_scan_msc = scan_msc
        return [item[2] for item in candidates[:100]]

    def loop_once(self, *, today: date | None = None) -> int:
        self.reconcile()
        count = self.consume_once(today=today)
        now = time.monotonic()
        if now - self.last_heartbeat >= 5:
            self.heartbeat()
            self.last_heartbeat = now
        if self.config.get("report_path") and now - self.last_account_report >= 10:
            self.write_account_report()
            self.last_account_report = now
        return count


def math_floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    step_decimal = Decimal(str(step))
    units = (Decimal(str(value)) / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_decimal)


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("Config follower harus berupa object JSON")
    config = dict(config)
    required = ("nama", "terminal_path", "login", "password_env", "server", "folder_sinyal")
    missing = [key for key in required if key not in config]
    missing.extend(key for key in ("max_posisi_terbuka", "max_lot_total", "max_lot_per_order") if key not in config)
    if missing:
        raise ValueError("Config follower kurang field: " + ", ".join(dict.fromkeys(missing)))
    for key in ("nama", "terminal_path", "password_env", "server", "folder_sinyal"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"Config follower field '{key}' harus berupa string yang tidak kosong")
    login = config["login"]
    if isinstance(login, bool) or not str(login).isdigit() or int(login) <= 0:
        raise ValueError("Config follower field 'login' harus berupa angka positif")
    for key in ("dry_run", "aktif", "arah_terbalik", "izinkan_netting", "portable"):
        if key in config and not isinstance(config[key], bool):
            raise ValueError(f"Config follower field '{key}' harus boolean JSON")
    for key in ("max_lot_total", "max_lot_per_order"):
        if isinstance(config[key], bool):
            raise ValueError(f"Config follower field '{key}' harus berupa angka positif")
        try:
            value = float(config[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Config follower field '{key}' harus berupa angka positif") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Config follower field '{key}' harus berupa angka positif")
    positions_limit = config["max_posisi_terbuka"]
    if isinstance(positions_limit, bool) or not isinstance(positions_limit, int) or positions_limit <= 0:
        raise ValueError("Config follower field 'max_posisi_terbuka' harus integer positif")
    if config.get("mode_lot", "rasio") not in ("tetap", "rasio", "saldo"):
        raise ValueError("mode_lot harus salah satu dari tetap, rasio, saldo")
    if not isinstance(config.get("map_simbol", {}), dict):
        raise ValueError("map_simbol harus berupa object JSON")
    for master_symbol, follower_symbol in config.get("map_simbol", {}).items():
        if not isinstance(master_symbol, str) or not isinstance(follower_symbol, str) or not follower_symbol:
            raise ValueError("map_simbol harus berisi pasangan nama simbol string")
    return dict(config)


def load_config(path: str | Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_config(config)


def run(config: Mapping[str, Any], state_path: str | Path | None = None, mt5: Any | None = None) -> None:
    config = validate_config(config)
    password_name = str(config["password_env"])
    password = os.environ.get(password_name)
    if not password:
        raise RuntimeError(f"Environment variable password '{password_name}' belum diatur")
    mt5 = mt5 or _load_mt5()
    configured_state = config.get("state_path")
    engine = FollowerEngine(config, mt5, state_path or configured_state or Path("state") / f"{config['nama']}.json")
    backoffs = (1, 2, 5, 10, 30)
    attempt = 0
    while True:
        try:
            engine.connect(password)
            attempt = 0
            break
        except ConnectionError:
            engine.logger.exception("Koneksi follower gagal, mt5.last_error=%s", mt5.last_error())
            time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
            attempt += 1
    try:
        while True:
            terminal = mt5.terminal_info()
            if terminal is None or not _get(terminal, "connected", False):
                engine.logger.error("Terminal follower terputus; mencoba koneksi ulang")
                mt5.shutdown()
                while True:
                    try:
                        engine.connect(password)
                        attempt = 0
                        break
                    except ConnectionError:
                        engine.logger.exception("Koneksi ulang gagal: %s", mt5.last_error())
                        time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
                        attempt += 1
                continue
            engine.loop_once()
            time.sleep(max(1, int(config.get("polling_ms", 20))) / 1000)
    except KeyboardInterrupt:
        engine.logger.info("Follower dihentikan")
    finally:
        mt5.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    args = parser.parse_args()
    config = load_config(args.config)
    global logger
    logger = configure_logging(str(config["nama"]))
    run(config, config.get("state_path"))


if __name__ == "__main__":
    main()
