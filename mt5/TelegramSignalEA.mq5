#property strict
#property version   "1.105"
#property description "Executes Telegram signals, reports master MT5 balance, and publishes all open positions"

#define MAX_SIGNAL_TARGETS 20
#define SIGNAL_EXPIRY_SECONDS 3600
#define PENDING_ORDER_EXPIRY_MINUTES 60

#include <Trade/Trade.mqh>

enum ENUM_LOT_MODE
  {
   LOT_FIXED = 0,
   LOT_RISK_PERCENT = 1
  };

input string ServerURL = "http://127.0.0.1:8000";
input string ApiKey = "";
input int PollIntervalMs = 1000;
input long MagicNumber = 26093001;
input ENUM_LOT_MODE LotMode = LOT_RISK_PERCENT;
input double FixedLot = 0.01;
input double RiskPercent = 1.0; // Total risk budget per signal, split across its entries
input int MaxSlippage = 20;
input int MaxSpreadPoints = 80;
input bool AllowBuy = true;
input bool AllowSell = true;
input bool UseDefaultSLTP = true;
input int DefaultSLPoints = 500;
input int DefaultTPPoints = 1000;
input string SymbolSuffix = "";
input string SymbolMapCsv = ""; // Optional source-to-broker mapping, e.g. XAUUSD=XAUUSDc,US30=US30.cash
input string MarketWatchlistCsv = "XAUUSD,EURUSD,USDJPY,GBPUSD";
input double MaxMarketDeviationPct = 5.0;
input int MaxRetries = 3;

CTrade g_trade;
datetime g_last_heartbeat = 0;
datetime g_last_expiry_scan = 0;
string g_connection_status = "starting";
string g_last_signal = "none";
string g_last_signal_status = "idle";
string g_spread_failure_reason = "";
int g_signal_count = 0;
int g_wins = 0;
int g_losses = 0;
ulong g_last_master_snapshot_ms = 0;

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

bool HttpRequest(const string method, const string path, const string payload, string &response_body)
  {
   if(StringLen(ApiKey) < 24)
     {
      g_connection_status = "API key missing/short";
      return false;
     }
   string base = TrimTrailingSlash(ServerURL);
   string url = base + path;
   string headers = "Content-Type: application/json\r\nX-API-Key: " + ApiKey + "\r\n";
   char data[];
   char result[];
   string result_headers;
   if(StringLen(payload) > 0)
     {
      int count = StringToCharArray(payload, data, 0, WHOLE_ARRAY, CP_UTF8);
      if(count > 0)
         ArrayResize(data, count - 1);
     }
   else
      ArrayResize(data, 0);

   ResetLastError();
   int code = WebRequest(method, url, headers, 5000, data, result, result_headers);
   if(code < 200 || code >= 300)
     {
      int error = GetLastError();
      g_connection_status = StringFormat("HTTP %d / error %d", code, error);
      Print("[TelegramSignalEA] ", method, " ", path, " failed: ", g_connection_status);
      response_body = CharArrayToString(result, 0, -1, CP_UTF8);
      return false;
     }
   response_body = CharArrayToString(result, 0, -1, CP_UTF8);
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

bool AccountUsesCentSymbols()
  {
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   StringToUpper(currency);
   if(StringFind(currency, "CENT") >= 0)
      return true;
   string cent_currencies[] = {"USC", "USDC", "EUC", "GBC", "GBPC", "AUC", "AUDC", "CADC", "JPYC"};
   for(int i = 0; i < ArraySize(cent_currencies); i++)
      if(currency == cent_currencies[i])
         return true;

   string identity = AccountInfoString(ACCOUNT_SERVER) + " " + AccountInfoString(ACCOUNT_NAME) + " " +
                     TerminalInfoString(TERMINAL_COMPANY);
   StringToUpper(identity);
   return StringFind(identity, "CENT") >= 0;
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
      reason = "signal symbol is empty";
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

   string source_upper = source;
   StringToUpper(source_upper);
   // Telegram signals usually identify gold as XAUUSD. On a cent account,
   // prefer XAUUSDc even if other XAUUSD variants are also listed.
   if(source_upper == "XAUUSD" && AccountUsesCentSymbols() && TrySelectBrokerSymbol("XAUUSDc", resolved))
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
      if(AccountUsesCentSymbols() && StringFind(source_upper, "XAUUSD") == 0 &&
         TrySelectBrokerSymbol("XAUUSDc", resolved))
         return true;
      reason = StringFormat("symbol %s matches multiple broker instruments (%s); configure SymbolSuffix or SymbolMapCsv", source, match_list);
      return false;
     }
   reason = StringFormat("broker has no symbol matching %s; configure SymbolSuffix or SymbolMapCsv", source);
   return false;
  }

bool AppendMasterMarketQuote(const string base_symbol, string &quotes, int &count)
  {
   string symbol;
   string reason;
   if(!ResolveBrokerSymbol(base_symbol, SymbolSuffix, SymbolMapCsv, symbol, reason))
      return false;
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick) || tick.bid <= 0.0 || tick.ask <= 0.0 || tick.time_msc <= 0)
      return false;
   if(count > 0)
      quotes += ",";
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   quotes += StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%s,\"ask\":%s,\"time_msc\":%I64d}",
      JsonEscape(symbol),
      DoubleToString(tick.bid, digits),
      DoubleToString(tick.ask, digits),
      tick.time_msc);
   count++;
   return true;
  }

string MasterMarketQuotesJson()
  {
   string quotes = "[";
   int count = 0;
   string watchlist[];
   int watch_count = StringSplit(MarketWatchlistCsv, ',', watchlist);
   for(int i = 0; i < watch_count && i < 100; i++)
     {
      string requested = watchlist[i];
      StringTrimLeft(requested);
      StringTrimRight(requested);
      if(requested != "")
         AppendMasterMarketQuote(requested, quotes, count);
     }
   quotes += "]";
   return quotes;
  }

bool PublishMasterSnapshot()
  {
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || AccountInfoInteger(ACCOUNT_LOGIN) <= 0)
      return false;

   string positions = "[";
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double lots = PositionGetDouble(POSITION_VOLUME);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      string action = type == POSITION_TYPE_BUY ? "BUY" : "SELL";
      if(count > 0)
         positions += ",";
      positions += StringFormat(
         "{\"ticket\":\"%I64u\",\"symbol\":\"%s\",\"action\":\"%s\",\"lots\":%s,\"entry_price\":%s,\"sl\":%s,\"tp\":%s}",
         ticket, JsonEscape(symbol), action,
         DoubleToString(lots, 8),
         DoubleToString(entry, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
         sl > 0.0 ? DoubleToString(sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) : "null",
         tp > 0.0 ? DoubleToString(tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) : "null");
      count++;
     }
   positions += "]";

   if(count > 500)
     {
      g_connection_status = "too many positions for copy snapshot";
      return false;
     }

   string terminal_login = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN));
   string terminal_server = AccountInfoString(ACCOUNT_SERVER);
   string currency = JsonEscape(AccountInfoString(ACCOUNT_CURRENCY));
   string market_quotes = MasterMarketQuotesJson();
   string payload = StringFormat(
      "{\"terminal_login\":\"%s\",\"terminal_server\":\"%s\",\"balance\":%s,\"equity\":%s,\"floating_profit\":%s,\"currency\":\"%s\",\"trade_mode\":\"%s\",\"market_quotes\":%s,\"positions\":%s}",
      terminal_login, JsonEscape(terminal_server),
      DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 8),
      DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 8),
      DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 8),
      currency, AccountTradeModeText(), market_quotes, positions);
   string response;
   bool published = HttpRequest("POST", "/api/ea/master/snapshot", payload, response);
   g_last_master_snapshot_ms = GetTickCount64();
   return published;
  }

int JsonValueStart(const string json, const string key)
  {
   string needle = "\"" + key + "\"";
   int key_pos = StringFind(json, needle);
   if(key_pos < 0)
      return -1;
   int colon = StringFind(json, ":", key_pos + StringLen(needle));
   if(colon < 0)
      return -1;
   int pos = colon + 1;
   while(pos < StringLen(json))
     {
      ushort c = StringGetCharacter(json, pos);
      if(c != 32 && c != 9 && c != 10 && c != 13)
         break;
      pos++;
     }
   return pos;
  }

string JsonValue(const string json, const string key)
  {
   int start = JsonValueStart(json, key);
   if(start < 0 || start >= StringLen(json))
      return "";
   ushort first = StringGetCharacter(json, start);
   if(first == '"')
     {
      bool escaped = false;
      for(int i = start + 1; i < StringLen(json); i++)
        {
         ushort c = StringGetCharacter(json, i);
         if(c == '"' && !escaped)
            return StringSubstr(json, start + 1, i - start - 1);
         if(c == '\\' && !escaped)
            escaped = true;
         else
            escaped = false;
        }
      return "";
     }
   if(first == '[' || first == '{')
     {
      int depth = 0;
      bool in_string = false;
      bool escaped = false;
      for(int i = start; i < StringLen(json); i++)
        {
         ushort c = StringGetCharacter(json, i);
         if(c == '"' && !escaped)
            in_string = !in_string;
         if(!in_string)
           {
            if(c == first)
               depth++;
            else if((first == '[' && c == ']') || (first == '{' && c == '}'))
              {
               depth--;
               if(depth == 0)
                  return StringSubstr(json, start, i - start + 1);
              }
           }
         if(c == '\\' && !escaped)
            escaped = true;
         else
            escaped = false;
        }
      return "";
     }
   int end = start;
   while(end < StringLen(json))
     {
      ushort c = StringGetCharacter(json, end);
      if(c == ',' || c == '}' || c == ']' || c == 10 || c == 13)
         break;
      end++;
     }
   return StringSubstr(json, start, end - start);
  }

string FirstSignalObject(const string response)
  {
   int items = StringFind(response, "\"items\"");
   if(items < 0)
      return "";
   int start = StringFind(response, "{", items);
   if(start < 0)
      return "";
   int depth = 0;
   bool in_string = false;
   bool escaped = false;
   for(int i = start; i < StringLen(response); i++)
     {
      ushort c = StringGetCharacter(response, i);
      if(c == '"' && !escaped)
         in_string = !in_string;
      if(!in_string)
        {
         if(c == '{')
            depth++;
         else if(c == '}')
           {
            depth--;
            if(depth == 0)
               return StringSubstr(response, start, i - start + 1);
           }
        }
      if(c == '\\' && !escaped)
         escaped = true;
      else
         escaped = false;
     }
   return "";
  }

double JsonNumber(const string json, const string key)
  {
   string value = JsonValue(json, key);
   if(value == "" || value == "null")
      return 0.0;
   return StringToDouble(value);
  }

long JsonLong(const string json, const string key)
  {
   string value = JsonValue(json, key);
   if(value == "" || value == "null")
      return 0;
   return (long)StringToInteger(value);
  }

int ArrayNumberCount(const string array_json)
  {
   if(StringLen(array_json) < 3)
      return 0;
   string values = StringSubstr(array_json, 1, StringLen(array_json) - 2);
   int start = 0;
   int count = 0;
   while(start < StringLen(values))
     {
      int comma = StringFind(values, ",", start);
      string token = comma < 0 ? StringSubstr(values, start) : StringSubstr(values, start, comma - start);
      double value = StringToDouble(token);
      if(value > 0.0)
        {
         count++;
         if(count >= MAX_SIGNAL_TARGETS)
            return count;
        }
      if(comma < 0)
         break;
      start = comma + 1;
     }
   return count;
  }

double ArrayNumberAt(const string array_json, const int target_index)
  {
   if(target_index < 0 || StringLen(array_json) < 3)
      return 0.0;
   string values = StringSubstr(array_json, 1, StringLen(array_json) - 2);
   int start = 0;
   int index = 0;
   while(start < StringLen(values))
     {
      int comma = StringFind(values, ",", start);
      string token = comma < 0 ? StringSubstr(values, start) : StringSubstr(values, start, comma - start);
      double value = StringToDouble(token);
      if(value > 0.0)
        {
         if(index == target_index)
            return value;
         index++;
         if(index >= MAX_SIGNAL_TARGETS)
            return 0.0;
        }
      if(comma < 0)
         break;
      start = comma + 1;
     }
   return 0.0;
  }

string SignalMarker(const long signal_id, const int leg=0)
  {
   string marker = "CTS:" + IntegerToString(signal_id);
   if(leg > 0)
      marker += ":" + IntegerToString(leg);
   return marker;
  }

long SignalIdFromComment(const string comment)
  {
   if(StringFind(comment, "CTS:") != 0)
      return 0;
   int separator = StringFind(comment, ":", 4);
   string value = separator < 0 ? StringSubstr(comment, 4) : StringSubstr(comment, 4, separator - 4);
   return (long)StringToInteger(value);
  }

string GlobalKey(const long signal_id, const string field, const int leg=0)
  {
   string suffix = field;
   if(leg > 0)
      suffix += IntegerToString(leg);
   return StringFormat("CT_%I64d_%I64d_%I64d_%s", AccountInfoInteger(ACCOUNT_LOGIN), MagicNumber, signal_id, suffix);
  }

int SavedState(const long signal_id, const int leg=0)
  {
   string key = GlobalKey(signal_id, "S", leg);
   if(!GlobalVariableCheck(key))
      return 0;
   return (int)GlobalVariableGet(key);
  }

void SaveReport(const long signal_id, const int state, const long ticket, const double price, const double lots, const int leg=0)
  {
   GlobalVariableSet(GlobalKey(signal_id, "S", leg), state);
   GlobalVariableSet(GlobalKey(signal_id, "T", leg), (double)ticket);
   GlobalVariableSet(GlobalKey(signal_id, "P", leg), price);
   GlobalVariableSet(GlobalKey(signal_id, "L", leg), lots);
  }

bool SendReport(const long signal_id, const string status, const long ticket, const string symbol,
                const string action, const double lots, const double price, const string reason, const int leg=0)
  {
   string body = StringFormat(
      "{\"signal_id\":%I64d,\"status\":\"%s\",\"ticket\":\"%I64d\",\"symbol\":\"%s\",\"action\":\"%s\",\"lots\":%.8f,\"exec_price\":%.8f,\"reason\":\"%s\",\"leg\":%d}",
      signal_id, status, ticket, JsonEscape(symbol), action, lots, price, JsonEscape(reason), leg);
   string response;
   return HttpRequest("POST", "/api/ea/report", body, response);
  }

void RepeatSavedReport(const long signal_id, const string symbol, const string action, const int leg=0)
  {
   int state = SavedState(signal_id, leg);
   if(state == 0)
      return;
   long ticket = (long)GlobalVariableGet(GlobalKey(signal_id, "T", leg));
   double price = GlobalVariableGet(GlobalKey(signal_id, "P", leg));
   double lots = GlobalVariableGet(GlobalKey(signal_id, "L", leg));
   if(state == 4)
      return;
   string status = state == 1 ? "EXECUTED" : state == 3 ? "FAILED" : "REJECTED";
   SendReport(signal_id, status, ticket, symbol, action, lots, price, "Idempotent report retry", leg);
  }

bool FindExistingSignalOrder(const long signal_id, long &ticket_out, double &lots_out, double &price_out, const int leg=0)
  {
   string marker = SignalMarker(signal_id, leg);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if((long)OrderGetInteger(ORDER_MAGIC) == MagicNumber && StringFind(OrderGetString(ORDER_COMMENT), marker) == 0)
        {
         ticket_out = (long)ticket;
         lots_out = OrderGetDouble(ORDER_VOLUME_CURRENT);
         price_out = OrderGetDouble(ORDER_PRICE_OPEN);
         return true;
        }
     }
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if((long)PositionGetInteger(POSITION_MAGIC) == MagicNumber && StringFind(PositionGetString(POSITION_COMMENT), marker) == 0)
        {
         ticket_out = (long)ticket;
         lots_out = PositionGetDouble(POSITION_VOLUME);
         price_out = PositionGetDouble(POSITION_PRICE_OPEN);
         return true;
        }
     }
   return false;
  }

bool IsXauUsdSymbol(const string symbol)
  {
   string normalized = symbol;
   StringToUpper(normalized);
   return StringFind(normalized, "XAUUSD") == 0;
  }

double GoldPointMultiplier(const string symbol)
  {
   if(!IsXauUsdSymbol(symbol))
      return 1.0;
   string normalized = symbol;
   StringToUpper(normalized);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(point <= 0.0)
      return 1.0;
   // Keep gold point-based inputs comparable when a cent account quotes
   // XAUUSDc with one extra decimal place.
   if(AccountUsesCentSymbols() || normalized == "XAUUSDC")
      return MathMax(1.0, 0.01 / point);
   return 1.0;
  }

int EffectiveGoldPointLimit(const string symbol, const int configured_points)
  {
   if(configured_points <= 0)
      return configured_points;
   return (int)MathCeil((double)configured_points * GoldPointMultiplier(symbol));
  }

void SetLastSignalStatus(const string status, const long signal_id, const string detail="")
  {
   string message = status;
   if(detail != "")
      message += ": " + StringSubstr(detail, 0, 180);
   g_last_signal_status = StringFormat("#%I64d %s", signal_id, message);
  }

double NormalizePrice(const string symbol, const double price)
  {
   if(price <= 0.0)
      return 0.0;
   double tick = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(tick <= 0.0)
      tick = SymbolInfoDouble(symbol, SYMBOL_POINT);
   return NormalizeDouble(MathRound(price / tick) * tick, digits);
  }

double NormalizePriceInRange(const string symbol, const double price, const double lower, const double upper)
  {
   double tick = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(tick <= 0.0)
      tick = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(tick <= 0.0 || lower <= 0.0 || upper < lower)
      return 0.0;

   double min_tick = MathCeil(lower / tick - 1e-8);
   double max_tick = MathFloor(upper / tick + 1e-8);
   if(min_tick > max_tick)
      return 0.0;

   double price_tick = MathRound(price / tick);
   price_tick = MathMax(min_tick, MathMin(max_tick, price_tick));
   double normalized = NormalizeDouble(price_tick * tick, digits);
   if(normalized < lower - tick * 1e-8 || normalized > upper + tick * 1e-8)
      return 0.0;
   return normalized;
  }

int VolumePrecision(const double step)
  {
   for(int digits = 0; digits <= 8; digits++)
      if(MathAbs(step - NormalizeDouble(step, digits)) < 0.000000001)
         return digits;
   return 8;
  }

double NormalizeLots(const string symbol, double lots)
  {
   double min_lot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double max_lot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0 || min_lot <= 0.0 || max_lot < min_lot || lots < min_lot)
      return 0.0;
   double capped_lots = MathMin(lots, max_lot);
   double normalized = min_lot + MathFloor((capped_lots - min_lot + step * 0.000001) / step) * step;
   if(normalized > max_lot + step * 0.000001)
      normalized -= step;
   return NormalizeDouble(normalized, VolumePrecision(step));
  }

double MinimumBrokerLots(const string symbol)
  {
   double minimum = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   if(minimum <= 0.0)
      return 0.0;
   return NormalizeLots(symbol, minimum);
  }

double CalculateLots(const string symbol, const string action, const double price,
                     const double stop_loss, const double risk_money, string &reason)
  {
   reason = "";
   double min_lot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double max_lot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   if(min_lot <= 0.0 || max_lot < min_lot || step <= 0.0)
     {
      reason = StringFormat("Spesifikasi volume broker tidak valid (min %.4f, max %.4f, step %.4f)",
                            min_lot, max_lot, step);
      return 0.0;
     }
   if(LotMode == LOT_FIXED)
     {
      double fixed_lots = NormalizeLots(symbol, FixedLot);
      if(fixed_lots <= 0.0)
        {
         fixed_lots = MinimumBrokerLots(symbol);
         reason = StringFormat("FixedLot di bawah minimum; menggunakan lot minimum broker %.4f", fixed_lots);
        }
      return fixed_lots;
     }
   if(stop_loss <= 0.0)
     {
      reason = "SL tidak tersedia; RiskPercent memerlukan SL untuk menghitung lot";
      return 0.0;
     }
   if(price <= 0.0)
     {
      reason = "Harga entry tidak tersedia untuk menghitung lot";
      return 0.0;
     }
   if(risk_money <= 0.0)
     {
      double minimum_lots = MinimumBrokerLots(symbol);
      if(minimum_lots > 0.0)
        {
         reason = StringFormat("Anggaran risiko %.2f %s; menggunakan lot minimum broker %.4f",
                               risk_money, currency, minimum_lots);
         Print("[TelegramSignalEA] risk budget is zero; trying minimum broker lot symbol=", symbol,
               " lot=", DoubleToString(minimum_lots, 4),
               " equity=", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
         return minimum_lots;
        }
      reason = "Lot minimum broker tidak valid";
      return 0.0;
     }
   ENUM_ORDER_TYPE order_type = action == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double profit_at_stop = 0.0;
   ResetLastError();
   if(!OrderCalcProfit(order_type, symbol, 1.0, price, stop_loss, profit_at_stop))
     {
      int error = GetLastError();
      reason = StringFormat("OrderCalcProfit gagal untuk %s (entry %s, SL %s, error %d)",
                            symbol,
                            DoubleToString(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
                            DoubleToString(stop_loss, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
                            error);
      return 0.0;
     }
   double risk_per_lot = MathAbs(profit_at_stop);
   if(risk_per_lot <= 0.0)
     {
      reason = StringFormat("Estimasi risiko per lot tidak valid untuk %s (entry %s, SL %s)",
                            symbol,
                            DoubleToString(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
                            DoubleToString(stop_loss, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)));
      return 0.0;
     }
   double requested_lots = risk_money / risk_per_lot;
   if(requested_lots + step * 0.000001 < min_lot)
     {
      double minimum_lot_risk = risk_per_lot * min_lot;
      double minimum_lots = MinimumBrokerLots(symbol);
      if(minimum_lots > 0.0)
        {
         reason = StringFormat("Lot hasil risiko di bawah minimum broker; menggunakan %.4f (estimasi risiko %.2f %s)",
                               minimum_lots, minimum_lot_risk, currency);
         Print("[TelegramSignalEA] calculated lot is below broker minimum; trying minimum lot symbol=", symbol,
               " lot=", DoubleToString(minimum_lots, 4),
               " calculated_lot=", DoubleToString(requested_lots, 6));
         return minimum_lots;
        }
      reason = StringFormat("Lot minimum broker tidak valid (min %.4f, max %.4f, step %.4f)", min_lot, max_lot, step);
      Print("[TelegramSignalEA] lot sizing blocked symbol=", symbol,
            " account_currency=", currency,
            " equity=", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
            " RiskPercent=", DoubleToString(RiskPercent, 2),
            " risk_per_entry=", DoubleToString(risk_money, 2),
            " min_lot=", DoubleToString(min_lot, 4),
            " min_lot_risk=", DoubleToString(minimum_lot_risk, 2),
            " calculated_lot=", DoubleToString(requested_lots, 6));
      return 0.0;
     }
   double normalized_lots = NormalizeLots(symbol, requested_lots);
   if(normalized_lots <= 0.0)
      reason = StringFormat("Hasil lot %.6f tidak sesuai minimum %.4f, maksimum %.4f, atau step %.4f broker",
                            requested_lots, min_lot, max_lot, step);
   return normalized_lots;
  }

bool CheckSpread(const string symbol)
  {
   g_spread_failure_reason = "";
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(ask <= 0.0 || bid <= 0.0 || point <= 0.0)
     {
      g_spread_failure_reason = "quote XAUUSD tidak tersedia";
      return false;
     }
   if(MaxSpreadPoints <= 0)
      return true;
   double spread = (ask - bid) / point;
   int spread_limit = EffectiveGoldPointLimit(symbol, MaxSpreadPoints);
   if(spread > spread_limit)
     {
      g_spread_failure_reason = StringFormat("spread %.1f points melebihi batas %d points pada %s",
                                             spread, spread_limit, symbol);
      Print("[TelegramSignalEA] spread gate symbol=", symbol,
            " spread_points=", DoubleToString(spread, 1),
            " limit_points=", spread_limit,
            " point=", DoubleToString(point, 8));
      return false;
     }
   return true;
  }

bool CheckPriceLevels(const string symbol, const string action, const double reference, const double sl, const double tp)
  {
   if(reference <= 0.0)
      return false;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(point <= 0.0)
      point = 0.00001;
   double minimum = (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   if(action == "BUY")
     {
      if(sl > 0.0 && (sl >= reference || reference - sl < minimum)) return false;
      if(tp > 0.0 && (tp <= reference || tp - reference < minimum)) return false;
     }
   else
     {
      if(sl > 0.0 && (sl <= reference || sl - reference < minimum)) return false;
      if(tp > 0.0 && (tp >= reference || reference - tp < minimum)) return false;
     }
   return true;
  }

bool PlaceOrder(const string action, const string order_type, const string symbol, const double entry,
                const double sl, const double tp, const double lots, const long signal_id, const int leg)
  {
   string comment = SignalMarker(signal_id, leg);
   g_trade.SetExpertMagicNumber((ulong)MagicNumber);
   int slippage_limit = EffectiveGoldPointLimit(symbol, MaxSlippage);
   g_trade.SetDeviationInPoints((ulong)MathMax(0, slippage_limit));
   g_trade.SetTypeFillingBySymbol(symbol);
   bool placed = false;
   int expiration_modes = (int)SymbolInfoInteger(symbol, SYMBOL_EXPIRATION_MODE);
   ENUM_ORDER_TYPE_TIME time_type = (expiration_modes & SYMBOL_EXPIRATION_SPECIFIED) != 0 ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC;
   datetime expiration = TimeCurrent() + PENDING_ORDER_EXPIRY_MINUTES * 60;
   for(int attempt = 0; attempt < MathMax(1, MaxRetries); attempt++)
     {
      if(order_type == "MARKET")
        {
         if(action == "BUY") placed = g_trade.Buy(lots, symbol, 0.0, sl, tp, comment);
         else placed = g_trade.Sell(lots, symbol, 0.0, sl, tp, comment);
        }
      else if(order_type == "LIMIT")
        {
         if(action == "BUY") placed = g_trade.BuyLimit(lots, entry, symbol, sl, tp, time_type, expiration, comment);
         else placed = g_trade.SellLimit(lots, entry, symbol, sl, tp, time_type, expiration, comment);
        }
      else
        {
         if(action == "BUY") placed = g_trade.BuyStop(lots, entry, symbol, sl, tp, time_type, expiration, comment);
         else placed = g_trade.SellStop(lots, entry, symbol, sl, tp, time_type, expiration, comment);
        }
      uint retcode = g_trade.ResultRetcode();
      if(placed && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_PLACED || retcode == TRADE_RETCODE_DONE_PARTIAL))
         return true;
      Print("[TelegramSignalEA] order attempt=", attempt + 1,
            " symbol=", symbol,
            " retcode=", retcode,
            " comment=", g_trade.ResultRetcodeDescription());
      if(retcode == TRADE_RETCODE_NO_MONEY)
         break;
      if(attempt + 1 < MathMax(1, MaxRetries))
         Sleep(250 * (attempt + 1));
     }
   return false;
  }

string ExpiryReportKey(const ulong ticket)
  {
   return StringFormat("CTEXP_%I64d_%I64d_%I64d", AccountInfoInteger(ACCOUNT_LOGIN), MagicNumber, (long)ticket);
  }

void ReportExpiredOrder(const ulong ticket)
  {
   string key = ExpiryReportKey(ticket);
   if(GlobalVariableCheck(key))
      return;
   string body = StringFormat(
      "{\"ticket\":\"%I64d\",\"profit\":0.0,\"result\":\"EXPIRED\"}", (long)ticket);
   string response;
   if(HttpRequest("POST", "/api/ea/result", body, response))
      GlobalVariableSet(key, (double)TimeCurrent());
  }

void ExpirePendingOrders()
  {
   datetime now = TimeCurrent();
   int lifetime = PENDING_ORDER_EXPIRY_MINUTES * 60;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || (long)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;
      if(StringFind(OrderGetString(ORDER_COMMENT), "CTS:") != 0)
         continue;
      datetime setup_time = (datetime)OrderGetInteger(ORDER_TIME_SETUP);
      if(setup_time <= 0 || now - setup_time < lifetime)
         continue;
      long position_id = OrderGetInteger(ORDER_POSITION_ID);
      if(g_trade.OrderDelete(ticket) && g_trade.ResultRetcode() == TRADE_RETCODE_DONE)
        {
         Print("[TelegramSignalEA] pending order ", ticket, " expired after ", PENDING_ORDER_EXPIRY_MINUTES, " minutes");
         if(position_id <= 0)
            ReportExpiredOrder(ticket);
        }
      else
         Print("[TelegramSignalEA] could not expire pending order ", ticket, ": ", g_trade.ResultRetcodeDescription());
     }
  }

void ReportExpiredOrdersFromHistory()
  {
   datetime now = TimeCurrent();
   if(g_last_expiry_scan > 0 && now - g_last_expiry_scan < 30)
      return;
   g_last_expiry_scan = now;
   if(!HistorySelect(now - 30 * 24 * 60 * 60, now))
      return;
   for(int i = HistoryOrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = HistoryOrderGetTicket(i);
      if(ticket == 0 || (long)HistoryOrderGetInteger(ticket, ORDER_MAGIC) != MagicNumber)
         continue;
      if((ENUM_ORDER_STATE)HistoryOrderGetInteger(ticket, ORDER_STATE) != ORDER_STATE_EXPIRED)
         continue;
      if(StringFind(HistoryOrderGetString(ticket, ORDER_COMMENT), "CTS:") != 0)
         continue;
      if(HistoryOrderGetInteger(ticket, ORDER_POSITION_ID) <= 0)
         ReportExpiredOrder(ticket);
     }
  }

void RejectSignal(const long signal_id, const string symbol, const string action, const string reason, const int leg_count=1)
  {
   SetLastSignalStatus("REJECTED", signal_id, reason);
   int count = MathMax(1, leg_count);
   for(int i = 0; i < count; i++)
     {
      int leg = count > 1 ? i + 1 : 0;
      SaveReport(signal_id, 2, 0, 0.0, 0.0, leg);
      SendReport(signal_id, "REJECTED", 0, symbol, action, 0.0, 0.0, reason, leg);
     }
   Print("[TelegramSignalEA] signal ", signal_id, " rejected: ", reason);
  }

void DeferSignal(const long signal_id, const string symbol, const string reason)
  {
   g_last_signal = StringFormat("waiting %s #%I64d", symbol, signal_id);
   SetLastSignalStatus("WAITING; akan dicoba ulang", signal_id, reason);
   Print("[TelegramSignalEA] signal ", signal_id, " deferred: ", reason,
         "; it will be retried after the backend claim lease expires");
  }

void ReportLegOutcome(const long signal_id, const string symbol, const string action, const int leg,
                      const string status, const double lots, const string reason)
  {
   SetLastSignalStatus(status, signal_id, reason);
   int state = status == "FAILED" ? 3 : 2;
   SaveReport(signal_id, state, 0, 0.0, lots, leg);
   SendReport(signal_id, status, 0, symbol, action, lots, 0.0, reason, leg);
   Print("[TelegramSignalEA] signal ", signal_id, " entry ", leg, " ", status, ": ", reason);
  }

string ZoneOrderType(const string action, const double entry, const double bid, const double ask, const double tick_size)
  {
   double tolerance = tick_size > 0.0 ? tick_size * 0.5 : 0.00000001;
   if(action == "BUY")
     {
      if(entry < ask - tolerance) return "LIMIT";
      if(entry > ask + tolerance) return "STOP";
      return "MARKET";
     }
   if(entry > bid + tolerance) return "LIMIT";
   if(entry < bid - tolerance) return "STOP";
   return "MARKET";
  }

bool CheckRewardRisk(const string symbol, const double reference, const double sl, const double tp)
  {
   if(reference <= 0.0 || sl <= 0.0 || tp <= 0.0)
      return true;
   double tick = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick <= 0.0)
      tick = SymbolInfoDouble(symbol, SYMBOL_POINT);
   return MathAbs(reference - sl) <= MathAbs(tp - reference) + tick * 0.5;
  }

bool ValidateSignalPlan(const string symbol, const string action, const string requested_type,
                        const bool zone_order, const double requested_entry,
                        const double entry_low, const double entry_high, const double sl_input,
                        const string tp_array, const int tp_count, const int order_count,
                        const double bid, const double ask, string &reason)
  {
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tick = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick <= 0.0)
      tick = point;

   for(int i = 0; i < order_count; i++)
     {
      double leg_entry = requested_entry;
      double tp = tp_count > 0 ? ArrayNumberAt(tp_array, i) : 0.0;
      string order_type = requested_type;
      if(zone_order)
        {
         if(tp_count == 0)
            leg_entry = i == 0 ? entry_low : entry_high;
         else if(order_count == 1)
            leg_entry = (entry_low + entry_high) / 2.0;
         else if(action == "BUY")
            leg_entry = entry_low + (entry_high - entry_low) * (double)i / (order_count - 1);
         else
            leg_entry = entry_high - (entry_high - entry_low) * (double)i / (order_count - 1);
         leg_entry = NormalizePriceInRange(symbol, leg_entry, entry_low, entry_high);
         order_type = ZoneOrderType(action, leg_entry, bid, ask, tick);
        }

      if(order_type != "MARKET" && order_type != "LIMIT" && order_type != "STOP")
        {
         reason = "Order type tidak valid";
         return false;
        }
      bool market = order_type == "MARKET";
      double reference = market ? (action == "BUY" ? ask : bid) : leg_entry;
      if(reference <= 0.0)
        {
         reason = "Quote atau harga entry tidak tersedia";
         return false;
        }

      double sl = sl_input;
      if(UseDefaultSLTP && sl <= 0.0 && DefaultSLPoints > 0 && point > 0.0)
         sl = action == "BUY" ? reference - EffectiveGoldPointLimit(symbol, DefaultSLPoints) * point : reference + EffectiveGoldPointLimit(symbol, DefaultSLPoints) * point;
      if(UseDefaultSLTP && tp <= 0.0 && DefaultTPPoints > 0 && point > 0.0)
         tp = action == "BUY" ? reference + EffectiveGoldPointLimit(symbol, DefaultTPPoints) * point : reference - EffectiveGoldPointLimit(symbol, DefaultTPPoints) * point;
      sl = NormalizePrice(symbol, sl);
      tp = NormalizePrice(symbol, tp);
      if(!CheckPriceLevels(symbol, action, reference, sl, tp))
        {
         reason = "SL/TP invalid or inside broker stops level";
         return false;
        }
      if(!CheckRewardRisk(symbol, reference, sl, tp))
        {
         reason = "Jarak SL lebih besar daripada jarak TP";
         return false;
        }
     }
   return true;
  }

bool IsOppositePendingOrder(const ENUM_ORDER_TYPE type, const string action)
  {
   if(action == "BUY")
      return type == ORDER_TYPE_SELL_LIMIT || type == ORDER_TYPE_SELL_STOP || type == ORDER_TYPE_SELL_STOP_LIMIT;
   return type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_BUY_STOP_LIMIT;
  }

int CountOpposingPositions(const string symbol, const string action, double &floating_profit)
  {
   int count = 0;
   floating_profit = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || (long)PositionGetInteger(POSITION_MAGIC) != MagicNumber ||
         PositionGetString(POSITION_SYMBOL) != symbol)
         continue;
      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      bool opposite = (action == "BUY" && type == POSITION_TYPE_SELL) ||
                      (action == "SELL" && type == POSITION_TYPE_BUY);
      if(!opposite)
         continue;
      count++;
      floating_profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return count;
  }

bool ResolveOpposingExposure(const long signal_id, const string symbol, const string action, const int leg_count)
  {
   double floating_profit = 0.0;
   int opposing = CountOpposingPositions(symbol, action, floating_profit);
   if(opposing > 0 && floating_profit > 0.0)
     {
      string held_action = action == "BUY" ? "SELL" : "BUY";
      RejectSignal(signal_id, symbol, action,
                   StringFormat("Signal %s diabaikan karena %d posisi %s masih floating profit", action, opposing, held_action),
                   leg_count);
      return false;
     }

   if(opposing > 0)
     {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || (long)PositionGetInteger(POSITION_MAGIC) != MagicNumber ||
            PositionGetString(POSITION_SYMBOL) != symbol)
            continue;
         ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
         bool opposite = (action == "BUY" && type == POSITION_TYPE_SELL) ||
                         (action == "SELL" && type == POSITION_TYPE_BUY);
         if(!opposite)
            continue;
         if(!g_trade.PositionClose(ticket, (ulong)MaxSlippage) ||
            (g_trade.ResultRetcode() != TRADE_RETCODE_DONE && g_trade.ResultRetcode() != TRADE_RETCODE_DONE_PARTIAL))
            Print("[TelegramSignalEA] could not close opposing position ", ticket, ": ", g_trade.ResultRetcodeDescription());
        }
      floating_profit = 0.0;
      if(CountOpposingPositions(symbol, action, floating_profit) > 0)
        {
         DeferSignal(signal_id, symbol, "opposing EA positions remain open; retrying before sending the new signal");
         return false;
        }
     }

   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || (long)OrderGetInteger(ORDER_MAGIC) != MagicNumber ||
         OrderGetString(ORDER_SYMBOL) != symbol)
         continue;
      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(!IsOppositePendingOrder(type, action))
         continue;
      if(!g_trade.OrderDelete(ticket) || g_trade.ResultRetcode() != TRADE_RETCODE_DONE)
        {
         DeferSignal(signal_id, symbol, "opposing pending EA order could not be canceled; retrying");
         return false;
        }
     }
   return true;
  }

bool ProcessOrderLeg(const long signal_id, const string symbol, const string action,
                     const string requested_type, const bool zone_order, const double requested_entry,
                     const double sl_input, const double tp_input, const int leg, const double risk_money,
                     const double bid, const double ask)
  {
   int saved_state = SavedState(signal_id, leg);
   if(saved_state != 0)
     {
      RepeatSavedReport(signal_id, symbol, action, leg);
      return true;
     }

   long old_ticket = 0;
   double old_lots = 0.0;
   double old_price = 0.0;
   if(FindExistingSignalOrder(signal_id, old_ticket, old_lots, old_price, leg))
     {
      SaveReport(signal_id, 1, old_ticket, old_price, old_lots, leg);
      SendReport(signal_id, "EXECUTED", old_ticket, symbol, action, old_lots, old_price,
                 "Recovered existing order after restart", leg);
      return true;
     }

   string order_type = zone_order ? ZoneOrderType(action, NormalizePrice(symbol, requested_entry), bid, ask,
                                                   SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE)) : requested_type;
   if(order_type != "MARKET" && order_type != "LIMIT" && order_type != "STOP")
     {
      ReportLegOutcome(signal_id, symbol, action, leg, "REJECTED", 0.0, "Order type tidak valid");
      return false;
     }
   bool market = order_type == "MARKET";
   double current_price = action == "BUY" ? ask : bid;
   if(market && current_price <= 0.0)
     {
      DeferSignal(signal_id, symbol, "current market quote disappeared before order submission");
      return false;
     }
   double entry = market ? 0.0 : NormalizePrice(symbol, requested_entry);
   if(!market && entry <= 0.0)
     {
      ReportLegOutcome(signal_id, symbol, action, leg, "REJECTED", 0.0, "Missing or invalid pending entry price");
      return false;
     }
   if(market && requested_entry > 0.0 && current_price > 0.0 &&
      MathAbs(requested_entry - current_price) / current_price * 100.0 > MaxMarketDeviationPct)
     {
      ReportLegOutcome(signal_id, symbol, action, leg, "REJECTED", 0.0, "Market entry deviates too far from current price");
      return false;
     }

   double reference = market ? current_price : entry;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double sl = sl_input;
   double tp = tp_input;
   if(UseDefaultSLTP && point > 0.0)
     {
      if(sl <= 0.0 && DefaultSLPoints > 0)
         sl = action == "BUY" ? reference - EffectiveGoldPointLimit(symbol, DefaultSLPoints) * point : reference + EffectiveGoldPointLimit(symbol, DefaultSLPoints) * point;
      if(tp <= 0.0 && DefaultTPPoints > 0)
         tp = action == "BUY" ? reference + EffectiveGoldPointLimit(symbol, DefaultTPPoints) * point : reference - EffectiveGoldPointLimit(symbol, DefaultTPPoints) * point;
     }
   sl = NormalizePrice(symbol, sl);
   tp = NormalizePrice(symbol, tp);
   if(!CheckPriceLevels(symbol, action, reference, sl, tp))
     {
      ReportLegOutcome(signal_id, symbol, action, leg, "REJECTED", 0.0, "SL/TP invalid or inside broker stops level");
      return false;
     }
   if(!CheckRewardRisk(symbol, reference, sl, tp))
     {
      ReportLegOutcome(signal_id, symbol, action, leg, "REJECTED", 0.0, "Jarak SL lebih besar daripada jarak TP");
      return false;
     }

   string lot_error = "";
   double lots = CalculateLots(symbol, action, reference, sl, risk_money, lot_error);
   if(lots <= 0.0)
     {
      ReportLegOutcome(signal_id, symbol, action, leg, "REJECTED", 0.0,
                       lot_error != "" ? lot_error : "Perhitungan lot menghasilkan volume nol");
      return false;
     }
   if(!PlaceOrder(action, order_type, symbol, entry, sl, tp, lots, signal_id, leg))
     {
      uint initial_retcode = g_trade.ResultRetcode();
      double minimum_lots = MinimumBrokerLots(symbol);
      double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      bool minimum_retry = initial_retcode == TRADE_RETCODE_NO_MONEY && minimum_lots > 0.0 &&
                           step > 0.0 && lots > minimum_lots + step * 0.000001;
      if(minimum_retry)
        {
         Print("[TelegramSignalEA] broker rejected lot for margin; retrying once at minimum lot symbol=", symbol,
               " original_lot=", DoubleToString(lots, 4),
               " retry_lot=", DoubleToString(minimum_lots, 4));
         if(PlaceOrder(action, order_type, symbol, entry, sl, tp, minimum_lots, signal_id, leg))
            lots = minimum_lots;
         else
            minimum_retry = false;
        }
      if(!minimum_retry)
        {
         string failure = StringFormat("Order failed: %s (%u)", g_trade.ResultRetcodeDescription(), g_trade.ResultRetcode());
         ReportLegOutcome(signal_id, symbol, action, leg, "FAILED", lots, failure);
         return false;
        }
     }

   long ticket = (long)g_trade.ResultOrder();
   double fill_price = g_trade.ResultPrice();
   double fill_lots = g_trade.ResultVolume();
   if(ticket <= 0)
     {
      if(!FindExistingSignalOrder(signal_id, ticket, fill_lots, fill_price, leg) || ticket <= 0)
        {
         SetLastSignalStatus("ORDER_ACCEPTED; mengambil nomor order", signal_id,
                             StringFormat("simbol %s, lot %s", symbol, DoubleToString(fill_lots, 2)));
         Print("[TelegramSignalEA] signal ", signal_id, " entry ", leg,
               " accepted but ticket is not available yet; will recover by order marker");
         return false;
        }
     }
   if(fill_price <= 0.0)
      fill_price = market ? current_price : entry;
   if(fill_lots <= 0.0)
      fill_lots = lots;
   SaveReport(signal_id, 1, ticket, fill_price, fill_lots, leg);
   SetLastSignalStatus("EXECUTED", signal_id,
                       StringFormat("%s %s lot %s", action, symbol, DoubleToString(fill_lots, 2)));
   string execution_reason = StringFormat("Order accepted at entry %s",
                                          DoubleToString(market ? fill_price : entry,
                                                         (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)));
   SendReport(signal_id, "EXECUTED", ticket, symbol, action, fill_lots, fill_price, execution_reason, leg);
   Print("[TelegramSignalEA] signal ", signal_id, " entry ", leg, " executed ticket ", ticket,
         " at ", DoubleToString(fill_price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
         " lots ", DoubleToString(fill_lots, 2));
   return true;
  }

void ProcessSignal(const string item)
  {
   long signal_id = JsonLong(item, "signal_id");
   if(signal_id <= 0)
      return;
   string action = JsonValue(item, "action");
   string order_type = JsonValue(item, "order_type");
   string requested_symbol = JsonValue(item, "symbol");
   if(requested_symbol == "")
      requested_symbol = "XAUUSD";
   g_last_signal = StringFormat("%s %s #%I64d", action, requested_symbol, signal_id);
   SetLastSignalStatus("CLAIMED; memeriksa simbol dan izin trading", signal_id);
   if(SavedState(signal_id, 0) != 0)
     {
      SetLastSignalStatus("SUDAH DIPROSES; kirim ulang laporan", signal_id);
      RepeatSavedReport(signal_id, requested_symbol, action, 0);
      return;
     }
   if(!IsXauUsdSymbol(requested_symbol))
     {
      RejectSignal(signal_id, requested_symbol, action, "Only XAUUSD signals are accepted");
      return;
     }
   string symbol;
   string symbol_error;
   if(!ResolveBrokerSymbol(requested_symbol, SymbolSuffix, SymbolMapCsv, symbol, symbol_error))
     {
      for(int leg = 1; leg <= MAX_SIGNAL_TARGETS; leg++)
         if(SavedState(signal_id, leg) != 0)
            RepeatSavedReport(signal_id, requested_symbol, action, leg);
      DeferSignal(signal_id, requested_symbol, symbol_error);
      return;
     }
   g_last_signal = StringFormat("%s %s #%I64d", action, symbol, signal_id);
   SetLastSignalStatus("CLAIMED; menyiapkan order", signal_id,
                       StringFormat("simbol broker %s, mata uang akun %s", symbol, AccountInfoString(ACCOUNT_CURRENCY)));
   Print("[TelegramSignalEA] signal ", signal_id,
         " resolved symbol ", requested_symbol, " -> ", symbol,
         " account_currency=", AccountInfoString(ACCOUNT_CURRENCY),
         " cent_account=", AccountUsesCentSymbols() ? "true" : "false");

   double entry_low = JsonNumber(item, "entry_low");
   double entry_high = JsonNumber(item, "entry_high");
   bool has_low = entry_low > 0.0;
   bool has_high = entry_high > 0.0;
   bool zone_order = has_low && has_high;
   if(has_low != has_high)
     {
      RejectSignal(signal_id, symbol, action, "Entry zone requires both lower and upper prices");
      return;
     }
   if(zone_order && entry_low > entry_high)
     {
      double swap = entry_low;
      entry_low = entry_high;
      entry_high = swap;
     }
   if(zone_order && entry_low == entry_high)
     {
      RejectSignal(signal_id, symbol, action, "Entry zone prices must be different");
      return;
     }
   if(!zone_order && order_type == "AUTO")
     {
      RejectSignal(signal_id, symbol, action, "AUTO order requires an entry zone");
      return;
     }

   long created_epoch = JsonLong(item, "created_epoch");
   if(created_epoch > 0 && TimeGMT() - created_epoch > SIGNAL_EXPIRY_SECONDS)
     {
      RejectSignal(signal_id, symbol, action, "Signal expired");
      return;
     }
   if((action == "BUY" && !AllowBuy) || (action == "SELL" && !AllowSell) || (action != "BUY" && action != "SELL"))
     {
      RejectSignal(signal_id, symbol, action, "Action disabled or invalid");
      return;
     }

   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double sl = JsonNumber(item, "sl");
   string tp_array = JsonValue(item, "tp");
   int tp_count = ArrayNumberCount(tp_array);
   int order_count = tp_count > 0 ? tp_count : (zone_order ? 2 : 1);
   if(JsonValue(item, "entry_mode") == "SINGLE")
      order_count = 1;
   string plan_error = "";
   double requested_entry = zone_order ? 0.0 : JsonNumber(item, "entry");
   if(bid > 0.0 && ask > 0.0 &&
      !ValidateSignalPlan(symbol, action, order_type, zone_order, requested_entry,
                          entry_low, entry_high, sl, tp_array, tp_count, order_count,
                          bid, ask, plan_error))
     {
      RejectSignal(signal_id, symbol, action, plan_error, order_count);
      return;
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED) ||
      !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
     {
      DeferSignal(signal_id, symbol, "MT5 terminal, account, or expert trading permission is disabled");
      return;
     }
   if(!CheckSpread(symbol))
     {
      string spread_reason = g_spread_failure_reason != "" ? g_spread_failure_reason : "spread belum memenuhi batas trading";
      DeferSignal(signal_id, symbol, spread_reason);
      return;
     }

   double risk_money_per_leg = 0.0;
   if(LotMode == LOT_RISK_PERCENT && RiskPercent > 0.0)
     {
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      if(equity > 0.0)
         risk_money_per_leg = equity * RiskPercent / 100.0 / MathMax(1, order_count);
     }

   if(order_count > 1 && AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      RejectSignal(signal_id, symbol, action, "Multiple TP entries require an MT5 hedging account");
      return;
     }

   if(!ResolveOpposingExposure(signal_id, symbol, action, order_count))
      return;

   for(int i = 0; i < order_count; i++)
     {
      int leg = order_count > 1 ? i + 1 : 0;
      double tp = tp_count > 0 ? ArrayNumberAt(tp_array, i) : 0.0;
      if(zone_order)
        {
         double leg_entry = 0.0;
         if(order_count == 1)
            leg_entry = (entry_low + entry_high) / 2.0;
         else if(tp_count == 0)
            leg_entry = i == 0 ? entry_low : entry_high;
         else if(action == "BUY")
            leg_entry = entry_low + (entry_high - entry_low) * (double)i / (order_count - 1);
         else
            leg_entry = entry_high - (entry_high - entry_low) * (double)i / (order_count - 1);
         leg_entry = NormalizePriceInRange(symbol, leg_entry, entry_low, entry_high);
         ProcessOrderLeg(signal_id, symbol, action, "AUTO", true, leg_entry, sl, tp, leg, risk_money_per_leg, bid, ask);
        }
      else
         ProcessOrderLeg(signal_id, symbol, action, order_type, false, JsonNumber(item, "entry"), sl, tp,
                         leg, risk_money_per_leg, bid, ask);
     }
   g_signal_count++;
  }

void PollSignals()
  {
   string body;
   if(!HttpRequest("GET", "/api/ea/pending?limit=1", "", body))
      return;
   string item = FirstSignalObject(body);
   if(item == "")
      return;
   ProcessSignal(item);
  }

void SendHeartbeat()
  {
   string symbol = JsonEscape(_Symbol);
   string body = StringFormat(
      "{\"active\":true,\"terminal\":\"%s\",\"version\":\"1.105\",\"symbol\":\"%s\"}",
      JsonEscape(TerminalInfoString(TERMINAL_NAME)), symbol);
   string response;
   HttpRequest("POST", "/api/ea/heartbeat", body, response);
   g_last_heartbeat = TimeCurrent();
  }

bool PositionExistsByIdentifier(const long position_id)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && (long)PositionGetInteger(POSITION_IDENTIFIER) == position_id)
         return true;
     }
   return false;
  }

void ReportClosedPosition(const long position_id, const ulong closing_deal)
  {
   string id_key = StringFormat("CTPOS_%I64d_%I64d_S", AccountInfoInteger(ACCOUNT_LOGIN), position_id);
   string ticket_key = StringFormat("CTPOS_%I64d_%I64d_T", AccountInfoInteger(ACCOUNT_LOGIN), position_id);
   if(!GlobalVariableCheck(id_key) || PositionExistsByIdentifier(position_id))
      return;
   long signal_id = (long)GlobalVariableGet(id_key);
   long ticket = (long)GlobalVariableGet(ticket_key);
   if(signal_id <= 0 || ticket <= 0 || !HistorySelectByPosition((ulong)position_id))
      return;
   double profit = 0.0;
   datetime close_time = TimeCurrent();
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      profit += HistoryDealGetDouble(deal, DEAL_PROFIT);
      profit += HistoryDealGetDouble(deal, DEAL_SWAP);
      profit += HistoryDealGetDouble(deal, DEAL_COMMISSION);
      datetime deal_time = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
      if(deal_time > close_time || i == 0) close_time = deal_time;
     }
   string result = MathAbs(profit) < 0.005 ? "BE" : profit > 0.0 ? "WIN" : "LOSS";
   string iso_close_time = TimeToString(close_time, TIME_DATE | TIME_SECONDS);
   StringReplace(iso_close_time, ".", "-");
   StringReplace(iso_close_time, " ", "T");
   string payload = StringFormat(
      "{\"ticket\":\"%I64d\",\"profit\":%.2f,\"result\":\"%s\",\"closed_at\":\"%s\"}",
      ticket, profit, result, iso_close_time);
   string response;
   if(HttpRequest("POST", "/api/ea/result", payload, response))
     {
      if(result == "WIN") g_wins++;
      if(result == "LOSS") g_losses++;
      GlobalVariableDel(id_key);
      GlobalVariableDel(ticket_key);
     }
   Print("[TelegramSignalEA] position ", position_id, " closed: ", result, " profit ", DoubleToString(profit, 2));
  }

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0 || !HistoryDealSelect(trans.deal))
      return;
   long position_id = (long)HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry == DEAL_ENTRY_IN || entry == DEAL_ENTRY_INOUT)
     {
      string comment = HistoryDealGetString(trans.deal, DEAL_COMMENT);
      if(StringFind(comment, "CTS:") == 0)
        {
         long signal_id = SignalIdFromComment(comment);
         long ticket = (long)HistoryDealGetInteger(trans.deal, DEAL_ORDER);
         GlobalVariableSet(StringFormat("CTPOS_%I64d_%I64d_S", AccountInfoInteger(ACCOUNT_LOGIN), position_id), signal_id);
         GlobalVariableSet(StringFormat("CTPOS_%I64d_%I64d_T", AccountInfoInteger(ACCOUNT_LOGIN), position_id), (double)ticket);
        }
     }
   if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY || entry == DEAL_ENTRY_INOUT)
      ReportClosedPosition(position_id, trans.deal);
  }

void UpdatePanel()
  {
   int closed = g_wins + g_losses;
   double winrate = closed > 0 ? (double)g_wins / closed * 100.0 : 0.0;
    Comment("TelegramSignalEA 1.105\n",
           "Backend: ", g_connection_status, "\n",
           "Mode: LIVE\n",
           "Signals this session: ", g_signal_count, "\n",
           "Win / Loss: ", g_wins, " / ", g_losses, " (", DoubleToString(winrate, 1), "%)\n",
           "Last signal: ", g_last_signal, "\n",
           "Execution status: ", g_last_signal_status);
  }

int OnInit()
  {
   if(PollIntervalMs < 100)
     {
      Print("[TelegramSignalEA] PollIntervalMs must be >= 100");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_trade.SetExpertMagicNumber((ulong)MagicNumber);
   g_trade.SetDeviationInPoints((ulong)MaxSlippage);
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("[TelegramSignalEA] AutoTrading is off; live signals will wait and retry when MT5 trading is enabled.");
   EventSetMillisecondTimer(PollIntervalMs);
   SendHeartbeat();
   PublishMasterSnapshot();
   UpdatePanel();
   Print("[TelegramSignalEA] initialized; add ServerURL to MT5 WebRequest allow-list");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
   Print("[TelegramSignalEA] stopped, reason ", reason);
  }

void OnTimer()
  {
   ExpirePendingOrders();
   ReportExpiredOrdersFromHistory();
   PollSignals();
   ulong now_ms = GetTickCount64();
   if(now_ms - g_last_master_snapshot_ms >= 1000)
      PublishMasterSnapshot();
   if(TimeCurrent() - g_last_heartbeat >= 30)
      SendHeartbeat();
   UpdatePanel();
  }
