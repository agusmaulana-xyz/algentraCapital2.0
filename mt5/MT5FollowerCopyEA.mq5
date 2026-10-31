#property strict
#property version   "1.104"
#property description "Copies all published Algentra positions and reports MT5 account history"

#include <Trade/Trade.mqh>
#include "GoldSymbolResolver.mqh"

input string ServerURL = "http://127.0.0.1:8000";
input string AccountToken = "";
input int PollIntervalMs = 1000;
input long MagicNumber = 26100301;
input double VolumeMultiplier = 1.0;
input double MaxEntryDeviationPercent = 0.0; // 0 disables the late-entry gate for existing master positions
input int MaxSlippagePoints = 20;
input string SymbolSuffix = ""; // Optional mapping for non-gold instruments; XAUUSD resolves automatically
input string SymbolMapCsv = ""; // Optional non-gold mapping; XAUUSD is automatic

CTrade g_trade;
string g_connection_status = "starting";
string g_account_report_status = "waiting for MT5 report";
string g_last_copy_error = "";
datetime g_last_log = 0;
datetime g_last_report_attempt = 0;
long g_history_cursor = 0;
long g_history_cursor_msc = 0;

string TrimTrailingSlash(string value)
  {
   while(StringLen(value) > 0 && StringSubstr(value, StringLen(value) - 1, 1) == "/")
      value = StringSubstr(value, 0, StringLen(value) - 1);
   return value;
  }

string JsonEscape(string value)
  {
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", " ");
   StringReplace(value, "\n", " ");
   return value;
  }

int JsonValueStart(const string json, const string key)
  {
   string needle = "\"" + key + "\"";
   int key_pos = StringFind(json, needle);
   if(key_pos < 0) return -1;
   int colon = StringFind(json, ":", key_pos + StringLen(needle));
   if(colon < 0) return -1;
   int pos = colon + 1;
   while(pos < StringLen(json))
     {
      ushort c = StringGetCharacter(json, pos);
      if(c != 32 && c != 9 && c != 10 && c != 13) break;
      pos++;
     }
   return pos;
  }

string JsonStringValue(const string json, const string key)
  {
   int start = JsonValueStart(json, key);
   if(start < 0 || StringGetCharacter(json, start) != '"') return "";
   int end = StringFind(json, "\"", start + 1);
   if(end < 0) return "";
   return StringSubstr(json, start + 1, end - start - 1);
  }

double JsonNumberValue(const string json, const string key)
  {
   int start = JsonValueStart(json, key);
   if(start < 0 || StringSubstr(json, start, 4) == "null") return 0.0;
   int end = start;
   while(end < StringLen(json))
     {
      ushort c = StringGetCharacter(json, end);
      if((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E') end++;
      else break;
     }
   if(end == start) return 0.0;
   return StringToDouble(StringSubstr(json, start, end - start));
  }

bool JsonBoolValue(const string json, const string key)
  {
   int start = JsonValueStart(json, key);
   return start >= 0 && StringSubstr(json, start, 4) == "true";
  }

bool JsonObjects(const string json, string &objects[])
  {
   ArrayResize(objects, 0);
   int key_pos = StringFind(json, "\"positions\"");
   if(key_pos < 0) return false;
   int array_start = StringFind(json, "[", key_pos);
   if(array_start < 0) return false;
   int i = array_start + 1;
   while(i < StringLen(json))
     {
      ushort c = StringGetCharacter(json, i);
      if(c == ']') return true;
      if(c == ',' || c == 32 || c == 9 || c == 10 || c == 13) { i++; continue; }
      if(c != '{') return false;
      int object_start = i;
      int depth = 0;
      bool in_string = false;
      bool escaped = false;
      for(; i < StringLen(json); i++)
        {
         ushort ch = StringGetCharacter(json, i);
         if(in_string)
           {
            if(ch == '\\' && !escaped) escaped = true;
            else
              {
               if(ch == '"' && !escaped) in_string = false;
               escaped = false;
              }
            continue;
           }
         if(ch == '"') { in_string = true; continue; }
         if(ch == '{') depth++;
         if(ch == '}') depth--;
         if(depth == 0)
           {
            int size = ArraySize(objects);
            ArrayResize(objects, size + 1);
            objects[size] = StringSubstr(json, object_start, i - object_start + 1);
            i++;
            break;
           }
        }
      if(depth != 0) return false;
     }
   return false;
  }

bool HttpGet(string &response_body)
  {
   if(StringLen(AccountToken) < 32)
     {
      g_connection_status = "account token missing/short";
      return false;
     }
   string terminal_login = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));
   string terminal_server = AccountInfoString(ACCOUNT_SERVER);
   string headers = "X-Account-Token: " + AccountToken + "\r\n" +
                    "X-MT5-Login: " + terminal_login + "\r\nX-MT5-Server: " + terminal_server + "\r\n";
   char data[];
   char result[];
   string result_headers;
   ArrayResize(data, 0);
   ResetLastError();
   int code = WebRequest("GET", TrimTrailingSlash(ServerURL) + "/api/mt5/follower/positions",
                         headers, 5000, data, result, result_headers);
   response_body = CharArrayToString(result, 0, -1, CP_UTF8);
   if(code < 200 || code >= 300)
     {
      g_connection_status = StringFormat("HTTP %d / error %d", code, GetLastError());
      return false;
     }
   long server_cursor = StringToInteger(JsonStringValue(response_body, "history_cursor"));
   if(server_cursor > g_history_cursor)
      g_history_cursor = server_cursor;
   long server_cursor_msc = (long)JsonNumberValue(response_body, "history_cursor_msc");
   if(server_cursor_msc > g_history_cursor_msc)
      g_history_cursor_msc = server_cursor_msc;
   g_connection_status = "online";
   return true;
  }

string AccountTradeModeText()
  {
   long mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(mode == ACCOUNT_TRADE_MODE_REAL) return "real";
   if(mode == ACCOUNT_TRADE_MODE_CONTEST) return "contest";
   return "demo";
  }

bool TrySelectBrokerSymbol(const string candidate, string &resolved)
  {
   if(StringLen(candidate) == 0 || !SymbolSelect(candidate, true))
      return false;
   resolved = candidate;
   return true;
  }

bool ResolveBrokerSymbol(const string requested, const string suffix, const string mapping_csv,
                         string &resolved, string &reason)
  {
   resolved = "";
   reason = "";
   string source = requested;
   StringTrimLeft(source);
   StringTrimRight(source);
   if(StringLen(source) == 0)
     {
      reason = "master symbol is empty";
      return false;
     }

   // The source can carry the master's broker suffix. Normalize gold to the
   // canonical pair, then map it to this follower terminal's cached symbol.
   if(AlgentraIsGoldPairRequest(source))
      return AlgentraResolveGoldSymbol(resolved, reason);
   string normalized_source = AlgentraNormalizeSymbol(source);
   if(StringFind(normalized_source, "XAU") == 0 || StringFind(normalized_source, "GOLD") == 0)
     {
      reason = "source instrument is not the canonical XAUUSD spot pair";
      return false;
     }

   string mapped = "";
   string mappings[];
   int mapping_count = StringSplit(mapping_csv, ',', mappings);
   for(int i = 0; i < mapping_count; i++)
     {
      string item = mappings[i];
      StringTrimLeft(item);
      StringTrimRight(item);
      int equal_at = StringFind(item, "=");
      if(equal_at <= 0)
         continue;
      string from_symbol = StringSubstr(item, 0, equal_at);
      string to_symbol = StringSubstr(item, equal_at + 1);
      StringTrimLeft(from_symbol);
      StringTrimRight(from_symbol);
      StringTrimLeft(to_symbol);
      StringTrimRight(to_symbol);
      string from_upper = from_symbol;
      string source_upper = source;
      StringToUpper(from_upper);
      StringToUpper(source_upper);
      if(from_upper == source_upper && StringLen(to_symbol) > 0)
        {
         mapped = to_symbol;
         break;
        }
     }

   if(mapped != "")
     {
      if(TrySelectBrokerSymbol(mapped, resolved))
         return true;
      if(suffix != "" && TrySelectBrokerSymbol(mapped + suffix, resolved))
         return true;
     }
   if(suffix != "" && TrySelectBrokerSymbol(source + suffix, resolved))
      return true;
   if(TrySelectBrokerSymbol(source, resolved))
      return true;

   string search_for = mapped != "" ? mapped : source;
   string search_upper = search_for;
   StringToUpper(search_upper);
   int matches = 0;
   string match = "";
   string match_list = "";
   int symbols_total = SymbolsTotal(false);
   for(int i = 0; i < symbols_total; i++)
     {
      string candidate = SymbolName(i, false);
      string candidate_upper = candidate;
      StringToUpper(candidate_upper);
      if(candidate_upper == search_upper)
         continue;
      bool candidate_has_suffix = StringFind(candidate_upper, search_upper) == 0;
      bool candidate_is_base = StringLen(candidate_upper) >= 3 && StringFind(search_upper, candidate_upper) == 0;
      if(!candidate_has_suffix && !candidate_is_base)
         continue;
      if(!SymbolSelect(candidate, true))
         continue;
      matches++;
      match = candidate;
      if(matches <= 3)
         match_list += (matches == 1 ? "" : ", ") + candidate;
     }

   if(matches == 1)
     {
      resolved = match;
      return true;
     }
   if(matches > 1)
     {
      reason = StringFormat("master symbol %s matches multiple follower instruments (%s); configure SymbolSuffix or SymbolMapCsv", source, match_list);
      return false;
     }
   reason = StringFormat("no follower symbol matches master symbol %s; configure SymbolSuffix or SymbolMapCsv", source);
   return false;
  }

string DealEntryText(const ENUM_DEAL_ENTRY entry)
  {
   if(entry == DEAL_ENTRY_IN) return "IN";
   if(entry == DEAL_ENTRY_OUT) return "OUT";
   if(entry == DEAL_ENTRY_OUT_BY) return "OUT_BY";
   if(entry == DEAL_ENTRY_INOUT) return "INOUT";
   return "";
  }

string CurrentOpenPositionIds()
  {
   string ids = "[";
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(PositionGetTicket(i) == 0) continue;
      long identifier = PositionGetInteger(POSITION_IDENTIFIER);
      if(identifier <= 0) continue;
      if(count > 0) ids += ",";
      ids += StringFormat("\"%I64d\"", identifier);
      count++;
     }
   ids += "]";
   return ids;
  }

bool BuildHistoryDealsJson(const long cursor, const long cursor_msc, string &deals_json, long &batch_cursor)
  {
   deals_json = "[";
   batch_cursor = cursor;
   datetime from_time = cursor_msc > 0 ? (datetime)MathMax(0, cursor_msc / 1000 - 1) : 0;
   if(!HistorySelect(from_time, TimeCurrent())) return false;
   int count = 0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total && count < 100; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0 || ticket <= (ulong)MathMax(0, cursor)) continue;
      ENUM_DEAL_TYPE type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL) continue;
      ENUM_DEAL_ENTRY entry_type = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      string entry = DealEntryText(entry_type);
      if(entry == "") continue;
      long position_id = HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
      long time_msc = HistoryDealGetInteger(ticket, DEAL_TIME_MSC);
      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      string action = type == DEAL_TYPE_BUY ? "BUY" : "SELL";
      if(count > 0) deals_json += ",";
      deals_json += StringFormat(
         "{\"deal_ticket\":\"%I64u\",\"position_id\":\"%I64d\",\"time_msc\":%I64d,\"symbol\":\"%s\",\"action\":\"%s\",\"entry\":\"%s\",\"volume\":%s,\"price\":%s,\"profit\":%s,\"commission\":%s,\"swap\":%s,\"fee\":%s}",
         ticket, position_id, time_msc, JsonEscape(symbol), action, entry,
         DoubleToString(HistoryDealGetDouble(ticket, DEAL_VOLUME), 8),
         DoubleToString(HistoryDealGetDouble(ticket, DEAL_PRICE), 8),
         DoubleToString(HistoryDealGetDouble(ticket, DEAL_PROFIT), 8),
         DoubleToString(HistoryDealGetDouble(ticket, DEAL_COMMISSION), 8),
         DoubleToString(HistoryDealGetDouble(ticket, DEAL_SWAP), 8),
         DoubleToString(HistoryDealGetDouble(ticket, DEAL_FEE), 8));
      batch_cursor = (long)ticket;
      count++;
     }
   deals_json += "]";
   return true;
  }

bool PostAccountReport(const string payload, string &response_body)
  {
   string terminal_login = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));
   string terminal_server = AccountInfoString(ACCOUNT_SERVER);
   string headers = "Content-Type: application/json\r\nX-Account-Token: " + AccountToken + "\r\n" +
                    "X-MT5-Login: " + terminal_login + "\r\nX-MT5-Server: " + terminal_server + "\r\n";
   char data[];
   char result[];
   string result_headers;
   int count = StringToCharArray(payload, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(count > 0) ArrayResize(data, count - 1);
   ResetLastError();
   int code = WebRequest("POST", TrimTrailingSlash(ServerURL) + "/api/mt5/follower/report",
                         headers, 5000, data, result, result_headers);
   response_body = CharArrayToString(result, 0, -1, CP_UTF8);
   if(code < 200 || code >= 300)
     {
      g_account_report_status = StringFormat("report HTTP %d / error %d", code, GetLastError());
      return false;
     }
   long server_cursor = StringToInteger(JsonStringValue(response_body, "history_cursor"));
   if(server_cursor > g_history_cursor) g_history_cursor = server_cursor;
   long server_cursor_msc = (long)JsonNumberValue(response_body, "history_cursor_msc");
   if(server_cursor_msc > g_history_cursor_msc) g_history_cursor_msc = server_cursor_msc;
   g_account_report_status = "saldo dan histori tersinkron";
   return true;
  }

void ReportAccountState()
  {
   datetime now = TimeCurrent();
   if(now - g_last_report_attempt < 5) return;
   g_last_report_attempt = now;
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_LOGIN) <= 0) return;

   string deals_json;
   long batch_cursor = g_history_cursor;
   if(!BuildHistoryDealsJson(g_history_cursor, g_history_cursor_msc, deals_json, batch_cursor))
     {
      g_account_report_status = "riwayat MT5 belum dapat dibaca";
      return;
     }
   string currency = JsonEscape(AccountInfoString(ACCOUNT_CURRENCY));
   string copy_error = JsonEscape(g_last_copy_error);
   string payload = StringFormat(
      "{\"balance\":%s,\"equity\":%s,\"floating_profit\":%s,\"margin\":%s,\"free_margin\":%s,\"currency\":\"%s\",\"trade_mode\":\"%s\",\"allow_live_trading\":%s,\"terminal_trade_allowed\":%s,\"expert_trade_allowed\":%s,\"open_position_ids\":%s,\"deals\":%s,\"copy_error\":\"%s\"}",
      DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 8),
      DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 8),
      DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 8),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 8),
      DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 8),
      currency, AccountTradeModeText(),
      "true",
      (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)) ? "true" : "false",
      (MQLInfoInteger(MQL_TRADE_ALLOWED) && AccountInfoInteger(ACCOUNT_TRADE_EXPERT)) ? "true" : "false",
      CurrentOpenPositionIds(), deals_json, copy_error);
   string response;
   PostAccountReport(payload, response);
  }

string Marker(const string source_ticket)
  {
   return "ACM:" + source_ticket;
  }

bool FindCopiedPositions(const string source_ticket, ulong &tickets[], double &volume)
  {
   ArrayResize(tickets, 0);
   volume = 0.0;
   string marker = Marker(source_ticket);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_COMMENT) != marker) continue;
      int size = ArraySize(tickets);
      ArrayResize(tickets, size + 1);
      tickets[size] = ticket;
      volume += PositionGetDouble(POSITION_VOLUME);
     }
   return ArraySize(tickets) > 0;
  }

int VolumePrecision(const double step)
  {
   for(int digits = 0; digits <= 8; digits++)
      if(MathAbs(step - NormalizeDouble(step, digits)) < 0.000000001) return digits;
   return 8;
  }

string MinimumLotFallbackKey(const string source_ticket)
  {
   return "ACMFB_" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + "_" + source_ticket;
  }

bool MinimumLotFallbackUsed(const string source_ticket)
  {
   return GlobalVariableCheck(MinimumLotFallbackKey(source_ticket));
  }

double NormalizeLots(const string symbol, const double requested)
  {
   double minimum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maximum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(minimum <= 0.0 || maximum <= 0.0 || step <= 0.0 || requested < minimum)
      return 0.0;
   double capped_request = MathMin(requested, maximum);
   double lots = minimum + MathFloor((capped_request - minimum + step * 0.000001) / step) * step;
   if(lots > maximum + step * 0.000001)
      lots -= step;
   return NormalizeDouble(lots, VolumePrecision(step));
  }

double MinimumBrokerLots(const string symbol)
  {
   double minimum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   if(minimum <= 0.0)
      return 0.0;
   return NormalizeLots(symbol, minimum);
  }

int CountManagedPositions()
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(PositionGetTicket(i) > 0 && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   return count;
  }

bool IsSourceTicket(const string ticket, string &source_tickets[])
  {
   for(int i = 0; i < ArraySize(source_tickets); i++)
      if(source_tickets[i] == ticket) return true;
   return false;
  }

void CloseMissingCopies(string &source_tickets[])
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong local_ticket = PositionGetTicket(i);
      if(local_ticket == 0 || PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      string comment = PositionGetString(POSITION_COMMENT);
      if(StringFind(comment, "ACM:") != 0) continue;
      string source_ticket = StringSubstr(comment, 4);
      if(IsSourceTicket(source_ticket, source_tickets)) continue;
      if(g_trade.PositionClose(local_ticket, MaxSlippagePoints))
        {
         GlobalVariableDel(MinimumLotFallbackKey(source_ticket));
         Print("[MT5FollowerCopyEA] source position ", source_ticket, " closed; Copy Trading position ", local_ticket, " closed");
        }
      else
         Print("[MT5FollowerCopyEA] failed to close Copy Trading position ", local_ticket, ": ", g_trade.ResultRetcodeDescription());
     }
  }

bool OpenCopy(const string source_ticket, const string symbol, const string action,
              const double lots, const double sl, const double tp, const bool allow_minimum_fallback)
  {
   MqlTick quote;
   if(!SymbolSelect(symbol, true) || !SymbolInfoTick(symbol, quote)) return false;
   g_trade.SetTypeFillingBySymbol(symbol);
   bool sent = action == "BUY"
               ? g_trade.Buy(lots, symbol, 0.0, sl, tp, Marker(source_ticket))
               : g_trade.Sell(lots, symbol, 0.0, sl, tp, Marker(source_ticket));
   uint retcode = g_trade.ResultRetcode();
   bool executed = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_DONE_PARTIAL);
   double executed_lots = lots;
   double minimum_lots = MinimumBrokerLots(symbol);
   if(!executed && allow_minimum_fallback && !MinimumLotFallbackUsed(source_ticket) &&
      retcode == TRADE_RETCODE_NO_MONEY && minimum_lots > 0.0 &&
      lots > minimum_lots + SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP) * 0.000001)
     {
      Print("[MT5FollowerCopyEA] broker menolak lot ", DoubleToString(lots, 2),
            " karena margin; mencoba lot minimum ", DoubleToString(minimum_lots, 2), " pada ", symbol);
      sent = action == "BUY"
             ? g_trade.Buy(minimum_lots, symbol, 0.0, sl, tp, Marker(source_ticket))
             : g_trade.Sell(minimum_lots, symbol, 0.0, sl, tp, Marker(source_ticket));
      retcode = g_trade.ResultRetcode();
      executed = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_DONE_PARTIAL);
      if(executed)
        {
         executed_lots = minimum_lots;
         GlobalVariableSet(MinimumLotFallbackKey(source_ticket), (double)TimeCurrent());
        }
     }
   if(!executed)
     {
      if(retcode == TRADE_RETCODE_NO_MONEY)
         g_last_copy_error = "Copy Trading gagal karena akun tidak memiliki saldo yang cukup. Silakan isi ulang saldo terlebih dahulu.";
      else if(retcode == TRADE_RETCODE_TRADE_DISABLED || retcode == TRADE_RETCODE_CLIENT_DISABLES_AT ||
              retcode == TRADE_RETCODE_SERVER_DISABLES_AT)
         g_last_copy_error = "Copy Trading gagal karena broker atau akun menolak izin trading. Periksa password trading dan izin Algo Trading.";
      else
         g_last_copy_error = "";
      Print("[MT5FollowerCopyEA] copy open failed for source ", source_ticket, ": ",
            g_last_copy_error == "" ? g_trade.ResultRetcodeDescription() : g_last_copy_error);
      return false;
     }
   g_last_copy_error = "";
   Print("[MT5FollowerCopyEA] copied ", action, " ", DoubleToString(executed_lots, 2), " ", symbol,
         " from source ", source_ticket);
   return true;
  }

void ReconcileVolumeAndStops(const string source_ticket, const string symbol, const string action,
                             const double requested_lots, const double sl, const double tp)
  {
   ulong local_tickets[];
   double current_volume = 0.0;
   FindCopiedPositions(source_ticket, local_tickets, current_volume);
   double requested_volume = requested_lots * VolumeMultiplier;
   double target_volume = NormalizeLots(symbol, requested_volume);
   double minimum_lots = MinimumBrokerLots(symbol);
   bool minimum_lot_target = false;
   if(MinimumLotFallbackUsed(source_ticket))
      target_volume = minimum_lots;
   else if(target_volume <= 0.0 && requested_volume > 0.0 && minimum_lots > 0.0)
     {
      target_volume = minimum_lots;
      minimum_lot_target = true;
      Print("[MT5FollowerCopyEA] source ", source_ticket, " lot di bawah minimum broker; mencoba lot ",
            DoubleToString(minimum_lots, 2), " pada ", symbol);
     }
   if(target_volume <= 0.0)
     {
      Print("[MT5FollowerCopyEA] source ", source_ticket, " skipped: Copy Trading lot is outside broker limits");
      return;
     }
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double tolerance = step * 0.51;

   if(ArraySize(local_tickets) == 0)
     {
      bool opened = OpenCopy(source_ticket, symbol, action, target_volume, sl, tp, true);
      if(opened && minimum_lot_target)
         GlobalVariableSet(MinimumLotFallbackKey(source_ticket), (double)TimeCurrent());
      return;
     }

   for(int i = 0; i < ArraySize(local_tickets); i++)
     {
      ulong ticket = local_tickets[i];
      if(!PositionSelectByTicket(ticket)) continue;
      double old_sl = PositionGetDouble(POSITION_SL);
      double old_tp = PositionGetDouble(POSITION_TP);
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      bool sl_changed = MathAbs(old_sl - sl) > point * 0.5;
      bool tp_changed = MathAbs(old_tp - tp) > point * 0.5;
      if((sl_changed || tp_changed) && !g_trade.PositionModify(ticket, sl, tp))
         Print("[MT5FollowerCopyEA] stop update failed for Copy Trading position ", ticket, ": ", g_trade.ResultRetcodeDescription());
     }

   current_volume = 0.0;
   for(int i = 0; i < ArraySize(local_tickets); i++)
      if(PositionSelectByTicket(local_tickets[i])) current_volume += PositionGetDouble(POSITION_VOLUME);

   if(target_volume + tolerance < current_volume)
     {
      double to_reduce = current_volume - target_volume;
      for(int i = 0; i < ArraySize(local_tickets) && to_reduce > tolerance; i++)
        {
         ulong ticket = local_tickets[i];
         if(!PositionSelectByTicket(ticket)) continue;
         double position_volume = PositionGetDouble(POSITION_VOLUME);
         double minimum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
         double reduction = MathMin(to_reduce, position_volume);
         if(reduction >= position_volume - tolerance)
           {
            if(g_trade.PositionClose(ticket, MaxSlippagePoints)) to_reduce -= position_volume;
           }
         else
           {
            reduction = NormalizeLots(symbol, reduction);
            if(reduction >= minimum && g_trade.PositionClosePartial(ticket, reduction, MaxSlippagePoints))
               to_reduce -= reduction;
           }
        }
     }
   else if(target_volume > current_volume + tolerance)
     {
      double extra = NormalizeLots(symbol, target_volume - current_volume);
      if(extra > 0.0) OpenCopy(source_ticket, symbol, action, extra, sl, tp, false);
     }
  }

void PollAndCopy()
  {
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_LOGIN) <= 0)
     {
      g_connection_status = "trading terminal disconnected";
      return;
     }
   string response;
   if(!HttpGet(response))
     {
      if(TimeCurrent() - g_last_log >= 15)
        {
         Print("[MT5FollowerCopyEA] backend request failed: ", g_connection_status);
         g_last_log = TimeCurrent();
        }
      return;
     }
   if(!JsonBoolValue(response, "active"))
     {
      g_connection_status = "waiting for fresh master snapshot";
      return;
     }

   string objects[];
   if(!JsonObjects(response, objects))
     {
      g_connection_status = "invalid master snapshot";
      return;
     }
   string source_tickets[];
   int total = ArraySize(objects);
   ArrayResize(source_tickets, total);
   for(int i = 0; i < total; i++)
      source_tickets[i] = JsonStringValue(objects[i], "ticket");

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED) ||
      !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
     {
      g_connection_status = "connected / trading not allowed by terminal";
      return;
     }
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      g_connection_status = "Copy Trading account must use hedging mode";
      return;
     }
   g_trade.SetExpertMagicNumber(MagicNumber);
   g_trade.SetDeviationInPoints(MaxSlippagePoints);

   for(int i = 0; i < total; i++)
     {
      string object = objects[i];
      string source_ticket = JsonStringValue(object, "ticket");
      string source_symbol = JsonStringValue(object, "symbol");
      string symbol;
      string symbol_error;
      string action = JsonStringValue(object, "action");
      double lots = JsonNumberValue(object, "lots");
      double entry = JsonNumberValue(object, "entry_price");
      double sl = JsonNumberValue(object, "sl");
      double tp = JsonNumberValue(object, "tp");
      if(source_ticket == "" || source_symbol == "" || (action != "BUY" && action != "SELL") || lots <= 0.0)
         continue;
      if(!ResolveBrokerSymbol(source_symbol, SymbolSuffix, SymbolMapCsv, symbol, symbol_error))
        {
         if(TimeCurrent() - g_last_log >= 15)
           {
            Print("[MT5FollowerCopyEA] source ", source_ticket, " waiting: ", symbol_error);
            g_last_log = TimeCurrent();
           }
         continue;
        }

      ulong local_tickets[];
      double local_volume = 0.0;
      bool already_copied = FindCopiedPositions(source_ticket, local_tickets, local_volume);
      MqlTick quote;
      if(!SymbolInfoTick(symbol, quote) || quote.ask <= 0.0 || quote.bid <= 0.0)
        {
         if(TimeCurrent() - g_last_log >= 15)
           {
            Print("[MT5FollowerCopyEA] source ", source_ticket, " waiting: no live quote for ", symbol);
            g_last_log = TimeCurrent();
           }
         continue;
        }
      double current_price = action == "BUY" ? quote.ask : quote.bid;
      if(!already_copied && MaxEntryDeviationPercent > 0.0 && entry > 0.0 &&
         MathAbs(current_price - entry) / entry * 100.0 > MaxEntryDeviationPercent)
        {
         if(TimeCurrent() - g_last_log >= 15)
           {
            Print("[MT5FollowerCopyEA] source ", source_ticket, " skipped: entry price deviation exceeded");
            g_last_log = TimeCurrent();
           }
         continue;
        }
      ReconcileVolumeAndStops(source_ticket, symbol, action, lots, sl, tp);
     }
   CloseMissingCopies(source_tickets);
   g_connection_status = "copying / live";
  }

int OnInit()
  {
   if(PollIntervalMs < 500 || StringLen(AccountToken) < 32 || VolumeMultiplier <= 0.0 ||
      MaxEntryDeviationPercent < 0.0)
      return INIT_PARAMETERS_INCORRECT;
   EventSetMillisecondTimer(PollIntervalMs);
   ReportAccountState();
   PollAndCopy();
   Print("[MT5FollowerCopyEA] initialized; live copying is enabled when MT5 trading permissions are enabled. Allow ServerURL in MT5 WebRequest options.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
   Print("[MT5FollowerCopyEA] stopped, reason ", reason);
  }

void OnTimer()
  {
   ReportAccountState();
   PollAndCopy();
   Comment("Algentra MT5 Copy Trading\nBackend: ", g_connection_status,
           "\nManaged positions: ", CountManagedPositions(),
           "\nSaldo: ", AccountInfoString(ACCOUNT_CURRENCY), " ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
           "\nCopying permission: ON",
           "\nStatus order: ", g_last_copy_error == "" ? "normal" : g_last_copy_error,
           "\nLaporan akun: ", g_account_report_status);
  }
