#property strict
#property version   "1.009"
#property description "Reports isolated MT5 terminal login and trade readiness to the managed worker."

#include "..\..\..\..\mt5\GoldSymbolResolver.mqh"

input string ProbeStatusFileName = "algentra-login-probe.json";
bool g_status_write_error_logged = false;
string g_last_status_log = "";
bool g_gold_chart_ready = false;
string g_gold_symbol_candidate = "";
string g_gold_symbol_reason = "";
datetime g_gold_chart_last_log = 0;
datetime g_gold_chart_last_check = 0;

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
   FileWriteString(file, StringFormat("{\"ready\":%s,\"valid\":%s,\"message\":\"%s\",\"login\":%I64d,\"server\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"floating_profit\":%.2f,\"currency\":\"%s\",\"account_trade_allowed\":%s,\"terminal_trade_allowed\":%s,\"expert_trade_allowed\":%s,\"gold_symbol_status\":\"%s\",\"gold_symbol\":\"%s\"}",
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
                                      AccountInfoInteger(ACCOUNT_TRADE_EXPERT) && MQLInfoInteger(MQL_TRADE_ALLOWED) ? "true" : "false",
                                      g_gold_symbol_candidate == "" ? "UNAVAILABLE" : "AVAILABLE",
                                      EscapeJSON(g_gold_symbol_candidate)));
   FileFlush(file);
   FileClose(file);
  }

void WriteStatus(const bool ready, const bool valid, const string message)
  {
   string safe_message = EscapeJSON(message);
   string server = EscapeJSON(AccountInfoString(ACCOUNT_SERVER));
   string log_payload = StringFormat("{\"ready\":%s,\"valid\":%s,\"message\":\"%s\",\"login\":%I64d,\"server\":\"%s\",\"gold_symbol_status\":\"%s\",\"gold_symbol\":\"%s\"}",
                                  ready ? "true" : "false",
                                  valid ? "true" : "false",
                                  safe_message,
                                  AccountInfoInteger(ACCOUNT_LOGIN),
                                  server,
                                  g_gold_symbol_candidate == "" ? "UNAVAILABLE" : "AVAILABLE",
                                  EscapeJSON(g_gold_symbol_candidate));
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
   return AlgentraResolveGoldSymbol(resolved, g_gold_symbol_reason);
  }

void EnsureDefaultGoldChart()
  {
   if(g_gold_chart_ready || TimeCurrent() - g_gold_chart_last_check < 10)
      return;
   g_gold_chart_last_check = TimeCurrent();

   if(g_gold_symbol_candidate == "")
     {
      if(!ResolveGoldSymbol(g_gold_symbol_candidate))
        {
         if(TimeCurrent() - g_gold_chart_last_log >= 15)
           {
            Print("[MT5LoginProbe] simbol belum tersedia; broker=", AccountInfoString(ACCOUNT_SERVER),
                  "; status=UNAVAILABLE; ", g_gold_symbol_reason);
            g_gold_chart_last_log = TimeCurrent();
           }
         return;
        }
     }
   string gold_symbol = g_gold_symbol_candidate;
   if(_Symbol == gold_symbol && _Period == PERIOD_M1)
     {
      g_gold_chart_ready = true;
      return;
     }
   ResetLastError();
   if(ChartSetSymbolPeriod(0, gold_symbol, PERIOD_M1))
     {
      g_gold_chart_ready = true;
      Print("[MT5LoginProbe] simbol XAUUSD ditemukan: ", gold_symbol,
            "; chart dipilih dan EA probe dijalankan pada M1.");
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
   // Connection validation does not wait for symbol resolution. Resolve and select
   // the broker's available XAUUSD variant after the live session is reported.
   EnsureDefaultGoldChart();
   if(g_gold_symbol_candidate != "")
      message += " Simbol XAUUSD terdeteksi: " + g_gold_symbol_candidate + ".";
   else
      message += " Peringatan: simbol emas belum tersedia; login tetap berhasil dan resolver akan mencoba lagi. " + g_gold_symbol_reason;
   WriteStatus(true, true, message);
  }
