"""Run an isolated, bounded MetaTrader 5 login probe for the managed worker."""

from __future__ import annotations

import json
import sys
from typing import Any


def _result(valid: bool, message: str) -> None:
    sys.stdout.write(json.dumps({"valid": valid, "message": message}, ensure_ascii=False))
    sys.stdout.flush()


def main() -> None:
    try:
        request: dict[str, Any] = json.load(sys.stdin)
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except Exception:
        _result(False, "Worker tidak dapat memulai pemeriksaan Python API MT5.")
        return

    try:
        options = {
            "path": str(request["path"]),
            "login": int(request["login"]),
            "password": str(request["password"]),
            "server": str(request["server"]),
            "portable": True,
            "timeout": 30000,
        }
        if not mt5.initialize(**options):
            error = mt5.last_error()
            code = error[0] if isinstance(error, (tuple, list)) and error else None
            detail = str(error[1])[:120] if isinstance(error, (tuple, list)) and len(error) > 1 else ""
            if code == -6:
                message = "Login gagal. Periksa nomor akun, password trading, dan nama server broker."
            elif code == -10005:
                message = "MT5 tidak merespons Python API (IPC timeout). Terminal mungkin macet atau dipakai proses lain. Pastikan satu worker memakai runtime ini, lalu restart terminal. Error ini tidak memastikan password/server salah."
            elif code == -10003:
                message = "Python API tidak menemukan koneksi IPC ke terminal MT5. Pastikan terminal dapat dijalankan dan tidak sedang dipakai proses/worker lain."
            else:
                suffix = f" (kode {code}{': ' + detail if detail else ''})" if code is not None else ""
                message = f"Terminal MT5 gagal tersambung ke broker{suffix}. Periksa nama server broker dan koneksi jaringan VPS."
            _result(False, message[:240])
            return

        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None or terminal is None or not getattr(terminal, "connected", False):
            _result(False, "Login belum terhubung ke server broker. Periksa akun dan nama server.")
            return
        if str(getattr(account, "login", "")) != str(request["login"]):
            _result(False, "Terminal terhubung ke nomor akun yang berbeda dari yang dimasukkan.")
            return
        if not getattr(account, "trade_allowed", False):
            _result(False, "Login berhasil, tetapi broker menolak izin trading akun ini.")
            return
        if not getattr(terminal, "trade_allowed", False):
            _result(False, "Login berhasil, tetapi izin Auto Trading terminal belum aktif.")
            return
        _result(True, "Login broker berhasil dan terminal siap untuk copy trading.")
    except Exception:
        _result(False, "Worker tidak dapat memvalidasi login pada terminal MT5.")
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
