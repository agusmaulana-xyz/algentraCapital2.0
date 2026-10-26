#property strict
#property version   "1.006"
#property description "Reports isolated MT5 terminal login and trade readiness to the managed worker."

input string ProbeStatusFileName = "algentra-login-probe.json";
bool g_status_write_error_logged = false;
string g_last_status_log = "";
bool g_gold_chart_ready = false;
datetime g_gold_chart_last_log = 0;
datetime g_gold_chart_last_check = 0;

bool AccountUsesCentSymbols()
  {
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   StringToUpper(currency);
   if(StringFind(currency, "CENT") >= 0 || currency == "USC" || currency == "USDC" || currency == "EUC" ||
      currency == "GBC" || currency == "GBPC" || currency == "AUC" ||
      currency == "AUDC" || currency == "CADC" || currency == "JPYC")
      return true;
   string identity = AccountInfoString(ACCOUNT_SERVER) + " " +
                     AccountInfoString(ACCOUNT_COMPANY) + " " +
                     AccountInfoString(ACCOUNT_NAME);
   StringToUpper(identity);
   return StringFind(identity, "CENT") >= 0;
  }

int GoldVariantRank(const string symbol)
  {
   string upper = symbol;
   StringToUpper(upper);
   string suffix = StringSubstr(upper, 6);
   return suffix == "" ? 0 : 1;
  }

bool SelectKnownCentGoldSymbol(string &resolved)
  {
   resolved = "";
   string requested = "XAUUSDc";
   bool is_custom = false;
   if(SymbolExist(requested, is_custom) &&
      SymbolInfoInteger(requested, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_DISABLED)
     {
      ResetLastError();
      if(SymbolSelect(requested, true))
        {
         resolved = requested;
         Print("[MT5LoginProbe] cent gold symbol added to Market Watch: ", resolved);
         return true;
        }
      Print("[MT5LoginProbe] failed to add ", requested,
            " to Market Watch; error ", GetLastError());
     }

   // Symbol names are case-sensitive in MT5. Search the broker catalog for
   // the same exact XAUUSDc symbol with different letter casing.
   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
     {
      string candidate = SymbolName(i, false);
      string upper = candidate;
      StringToUpper(upper);
      if(upper != "XAUUSDC" ||
         SymbolInfoInteger(candidate, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_DISABLED)
         continue;
      ResetLastError();
      if(SymbolSelect(candidate, true))
        {
         resolved = candidate;
         Print("[MT5LoginProbe] cent gold symbol added to Market Watch: ", resolved);
         return true;
        }
      Print("[MT5LoginProbe] failed to add ", candidate,
            " to Market Watch; error ", GetLastError());
     }
   return false;
  }

string EscapeJSON(string value)
  {
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", " ");
   StringReplace(value, "\n", " ");
   return value;
  }

void WriteStatusToFile(const string file_name,
                       const bool ready,
                       const bool valid,
                       const string message,
                       const bool common)
  {
   int flags = FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE;
   if(common)
      flags |= FILE_COMMON;
   int file = FileOpen(file_name, flags);
   if(file == INVALID_HANDLE)
     {
      if(!g_status_write_error_logged)
        {
         Print("[MT5LoginProbe] cannot write ", common ? "common" : "portable",
               " status file ", file_name, "; error ", GetLastError());
         g_status_write_error_logged = true;
        }
      return;
     }
   g_status_write_error_logged = false;
   string safe_message = EscapeJSON(message);
   string server = EscapeJSON(AccountInfoString(ACCOUNT_SERVER));
   string currency = EscapeJSON(AccountInfoString(ACCOUNT_CURRENCY));
   FileWriteString(file, StringFormat("{\"ready\":%s,\"valid\":%s,\"message\":\"%s\",\"login\":%I64d,\"server\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"floating_profit\":%.2f,\"currency\":\"%s\",\"account_trade_allowed\":%s,\"terminal_trade_allowed\":%s,\"expert_trade_allowed\":%s}",
                                      ready ? "true" : "false",
                                      valid ? "true" : "false",
                                      safe_message,
                                      AccountInfoInteger(ACCOUNT_LOGIN),
                                      server,
                                      AccountInfoDouble(ACCOUNT_BALANCE),
                                      AccountInfoDouble(ACCOUNT_EQUITY),
                                      AccountInfoDouble(ACCOUNT_PROFIT),
                                      currency,
                                      AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false",
                                      TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false",
                                      AccountInfoInteger(ACCOUNT_TRADE_EXPERT) && MQLInfoInteger(MQL_TRADE_ALLOWED) ? "true" : "false"));
   FileFlush(file);
   FileClose(file);
  }

void WriteStatus(const bool ready, const bool valid, const string message)
  {
   string safe_message = EscapeJSON(message);
   string server = EscapeJSON(AccountInfoString(ACCOUNT_SERVER));
   string log_payload = StringFormat("{\"ready\":%s,\"valid\":%s,\"message\":\"%s\",\"login\":%I64d,\"server\":\"%s\"}",
                                  ready ? "true" : "false",
                                  valid ? "true" : "false",
                                  safe_message,
                                  AccountInfoInteger(ACCOUNT_LOGIN),
                                  server);
   // Give the worker a log-based fallback if terminal file access is blocked.
   if(log_payload != g_last_status_log)
     {
      Print("[MT5LoginProbe] STATUS ", log_payload);
      g_last_status_log = log_payload;
     }
   // The worker runs this terminal in portable mode, so its local MQL5/Files
   // directory is deterministic. Also write to FILE_COMMON for diagnostics and
   // compatibility with non-portable terminal layouts.
   WriteStatusToFile(ProbeStatusFileName, ready, valid, message, false);
   WriteStatusToFile(ProbeStatusFileName, ready, valid, message, true);
  }

bool ResolveGoldSymbol(string &resolved)
  {
   resolved = "";
   bool cent_account = AccountUsesCentSymbols();
   if(cent_account)
     {
      if(SelectKnownCentGoldSymbol(resolved))
         return true;
      Print("[MT5LoginProbe] akun cent membutuhkan simbol XAUUSDc, tetapi simbol itu tidak tersedia atau nonaktif.");
      return false;
     }

   string best = "";
   string best_upper = "";
   long best_quote_time = 0;
   int best_history = 0; // 1 when the broker actually serves M1 bars for this symbol
   int best_variant_rank = 2147483647;
   int best_suffix_length = 2147483647;
   int symbols_total = SymbolsTotal(false);
   for(int i = 0; i < symbols_total; i++)
     {
      string candidate = SymbolName(i, false);
      string upper = candidate;
      StringToUpper(upper);
      if(StringLen(candidate) < 6 || StringSubstr(upper, 0, 6) != "XAUUSD")
         continue;
      if(SymbolInfoInteger(candidate, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_DISABLED)
         continue;
      // Select matching gold variants so MT5 can load quotes for the account's
      // actual instrument before choosing between XAUUSD, XAUUSDc, XAUUSDm, etc.
      SymbolSelect(candidate, true);
      MqlTick tick;
      long quote_time = 0;
      if(SymbolInfoTick(candidate, tick) && tick.bid > 0.0 && tick.ask > 0.0)
         quote_time = tick.time_msc > 0 ? tick.time_msc : (long)tick.time * 1000;
      long server_now = (long)TimeTradeServer() * 1000;
      // On weekends/closed sessions, stale ticks cannot identify the account's
      // instrument. In that case prefer a broker suffix over the bare symbol.
      if(server_now > 0 && quote_time < server_now - 30 * 60 * 1000)
         quote_time = 0;
      int suffix_length = StringLen(candidate) - 6;
      int variant_rank = GoldVariantRank(candidate);
      // A symbol can exist in the broker catalog (e.g. bare XAUUSD) without the
      // account receiving any data for it, which gives a black chart. Symbols
      // that actually have bars always outrank those that do not.
      int history = SeriesInfoInteger(candidate, PERIOD_M1, SERIES_BARS_COUNT) > 0 ? 1 : 0;
      bool better = (best == "");
      if(best != "")
        {
         bool same_data = (history == best_history && quote_time == best_quote_time);
         if(quote_time > best_quote_time ||
            (quote_time == best_quote_time && history > best_history) ||
            (same_data && variant_rank < best_variant_rank) ||
            (same_data && variant_rank == best_variant_rank && suffix_length < best_suffix_length) ||
            (same_data && variant_rank == best_variant_rank && suffix_length == best_suffix_length && upper < best_upper))
            better = true;
        }
      if(better)
        {
         best = candidate;
         best_upper = upper;
         best_history = history;
         best_quote_time = quote_time;
         best_variant_rank = variant_rank;
         best_suffix_length = suffix_length;
        }
     }
   if(best == "" || !SymbolSelect(best, true))
      return false;
   resolved = best;
   return true;
  }

void EnsureDefaultGoldChart()
  {
   if(g_gold_chart_ready && TimeCurrent() - g_gold_chart_last_check < 10)
      return;
   g_gold_chart_last_check = TimeCurrent();

   string gold_symbol;
   if(!ResolveGoldSymbol(gold_symbol))
     {
      if(TimeCurrent() - g_gold_chart_last_log >= 15)
        {
         Print("[MT5LoginProbe] XAUUSD belum ditemukan di daftar simbol broker.");
         g_gold_chart_last_log = TimeCurrent();
        }
      return;
     }
   if(_Symbol == gold_symbol && _Period == PERIOD_M1)
     {
      g_gold_chart_ready = true;
      return;
     }
   ResetLastError();
   if(ChartSetSymbolPeriod(0, gold_symbol, PERIOD_M1))
     {
      g_gold_chart_ready = true;
      Print("[MT5LoginProbe] chart probe memakai ", gold_symbol, ", M1.");
     }
   else if(TimeCurrent() - g_gold_chart_last_log >= 15)
     {
      Print("[MT5LoginProbe] gagal mengganti chart ke ", gold_symbol,
            "; error ", GetLastError());
      g_gold_chart_last_log = TimeCurrent();
     }
  }

int OnInit()
  {
   WriteStatus(false, false, "Menunggu koneksi broker MT5.");
   if(!EventSetTimer(1)) return INIT_FAILED;
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_LOGIN) <= 0)
     {
      WriteStatus(false, false, "Menunggu koneksi broker MT5.");
      return;
     }
   string message = "Login broker berhasil dan terminal tersambung. Copy trading sedang standby.";
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
      message = "Login broker tersambung, tetapi akun read-only atau izin trading akun dinonaktifkan.";
   else if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      message = "Login broker tersambung, tetapi izin trading Expert Advisor belum aktif.";
   else if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      message = "Login broker tersambung, tetapi izin Auto Trading terminal belum aktif.";
   WriteStatus(true, true, message);
   // Connection validation does not wait for symbol resolution. Resolve and select
   // the account's XAUUSD or cent XAUUSDc after the live session is reported.
   EnsureDefaultGoldChart();
  }
