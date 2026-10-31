#ifndef ALGENTRA_GOLD_SYMBOL_RESOLVER_MQH
#define ALGENTRA_GOLD_SYMBOL_RESOLVER_MQH

// Canonical XAUUSD resolver shared by the MT5 EAs. The broker catalog is
// enumerated once per successful resolution; only the selected candidate is
// activated in Market Watch. A missing symbol is retried after ten seconds.
string g_algentra_gold_symbol = "";
datetime g_algentra_gold_last_attempt = 0;
datetime g_algentra_gold_last_log = 0;
long g_algentra_gold_login = 0;
string g_algentra_gold_server = "";

string AlgentraNormalizeSymbol(const string source)
  {
   string upper = source;
   StringToUpper(upper);
   string normalized = "";
   for(int i = 0; i < StringLen(upper); i++)
     {
      ushort c = StringGetCharacter(upper, i);
      if((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'))
         normalized += ShortToString(c);
     }
   return normalized;
  }

bool AlgentraIsCurrencyCode(const string value)
  {
   string currencies[] = {"AUD", "CAD", "CHF", "CNH", "CNY", "EUR", "GBP", "HKD", "JPY", "MXN",
                          "NOK", "NZD", "PLN", "RUB", "SEK", "SGD", "TRY", "USD", "ZAR"};
   for(int i = 0; i < ArraySize(currencies); i++)
      if(value == currencies[i])
         return true;
   return false;
  }

bool AlgentraIsGoldPairRequest(const string source)
  {
   string upper = source;
   StringToUpper(upper);
   if(StringFind(upper, "XAUUSD_O") == 0)
      return false;
   string normalized = AlgentraNormalizeSymbol(source);
   if(StringFind(normalized, "XAUUSD") == 0)
     {
      string suffix = StringSubstr(normalized, 6);
      if(StringLen(suffix) == 3 && AlgentraIsCurrencyCode(suffix))
         return false;
      if(StringFind(suffix, "FUT") >= 0 || StringFind(suffix, "FWD") >= 0 ||
         StringFind(suffix, "OPT") >= 0 || StringFind(suffix, "CFD") >= 0)
         return false;
      return true;
     }
   if(StringFind(normalized, "GOLD") == 0)
     {
      string suffix = StringSubstr(normalized, 4);
      return StringFind(suffix, "FUT") < 0 && StringFind(suffix, "FWD") < 0 &&
             StringFind(suffix, "OPT") < 0 && StringFind(suffix, "CFD") < 0;
     }
   return false;
  }

bool AlgentraHasMarker(const string value, const string marker)
  {
   return StringFind(value, marker) >= 0;
  }

bool AlgentraCandidateIsDerivative(const string candidate, const string suffix)
  {
   string upper = candidate;
   StringToUpper(upper);
   if(StringFind(upper, "XAUUSD_O") == 0)
      return true;
   string description = SymbolInfoString(candidate, SYMBOL_DESCRIPTION);
   string path = SymbolInfoString(candidate, SYMBOL_PATH);
   StringToUpper(description);
   StringToUpper(path);
   string metadata = suffix + " " + description + " " + path;
   if(AlgentraHasMarker(metadata, "FUT") || AlgentraHasMarker(metadata, "FWD") ||
      AlgentraHasMarker(metadata, "OPT") || AlgentraHasMarker(metadata, "BINARY") ||
      AlgentraHasMarker(suffix, "CFD"))
      return true;
   if(SymbolInfoInteger(candidate, SYMBOL_EXPIRATION_TIME) > 0)
      return true;
   long calculation_mode = SymbolInfoInteger(candidate, SYMBOL_TRADE_CALC_MODE);
   if(calculation_mode == SYMBOL_CALC_MODE_FUTURES ||
      calculation_mode == SYMBOL_CALC_MODE_EXCH_FUTURES)
      return true;
   return false;
  }

bool AlgentraCandidateIsGoldSpot(const string candidate, const string normalized,
                                 const bool allow_alias, int &suffix_length)
  {
   string upper_candidate = candidate;
   StringToUpper(upper_candidate);
   suffix_length = 0;
   bool xauusd_prefix = StringFind(normalized, "XAUUSD") == 0;
   bool gold_alias = StringFind(normalized, "GOLD") == 0;
   if(!xauusd_prefix && !(allow_alias && gold_alias))
      return false;

   string suffix = xauusd_prefix ? StringSubstr(normalized, 6) : StringSubstr(normalized, 4);
   if(xauusd_prefix && StringLen(suffix) == 3 && AlgentraIsCurrencyCode(suffix))
      return false;
   if(AlgentraCandidateIsDerivative(candidate, suffix))
      return false;

   string base = SymbolInfoString(candidate, SYMBOL_CURRENCY_BASE);
   string profit = SymbolInfoString(candidate, SYMBOL_CURRENCY_PROFIT);
   StringToUpper(base);
   StringToUpper(profit);
   if((base != "" && base != "XAU") || (profit != "" && profit != "USD"))
      return false;
   // GOLD aliases carry no reliable pair identity in their names, so both
   // currencies must be present and match the canonical spot pair.
   if(gold_alias && (base != "XAU" || profit != "USD"))
      return false;
   if(xauusd_prefix && StringFind(upper_candidate, "XAUUSD") == 0)
      suffix_length = StringLen(candidate) - 6;
   else if(gold_alias && StringFind(upper_candidate, "GOLD") == 0)
      suffix_length = StringLen(candidate) - 4;
   else
      suffix_length = StringLen(suffix);
   return true;
  }

struct AlgentraGoldCandidate
  {
   string name;
   int preferred;
   int suffix_length;
   int selected;
   int xauusd_prefix;
  };

bool AlgentraCandidateRanksBefore(const AlgentraGoldCandidate &left,
                                  const AlgentraGoldCandidate &right)
  {
   if(left.preferred != right.preferred)
      return left.preferred > right.preferred;
   if(left.suffix_length != right.suffix_length)
      return left.suffix_length < right.suffix_length;
   if(left.selected != right.selected)
      return left.selected > right.selected;
   return StringCompare(left.name, right.name, false) < 0;
  }

bool AlgentraResolveGoldSymbol(string &resolved, string &reason)
  {
   resolved = "";
   reason = "";
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   if(login != g_algentra_gold_login || server != g_algentra_gold_server)
     {
      g_algentra_gold_login = login;
      g_algentra_gold_server = server;
      g_algentra_gold_symbol = "";
      g_algentra_gold_last_attempt = 0;
      g_algentra_gold_last_log = 0;
     }
   if(g_algentra_gold_symbol != "")
     {
      resolved = g_algentra_gold_symbol;
      return true;
     }
   datetime now = TimeLocal();
   if(g_algentra_gold_last_attempt > 0 && now - g_algentra_gold_last_attempt < 10)
     {
      reason = "simbol emas belum tersedia; resolver akan mencoba kembali";
      return false;
     }
   g_algentra_gold_last_attempt = now;

   int total = SymbolsTotal(false);
   AlgentraGoldCandidate candidates[];
   string nearby = "";
   int nearby_count = 0;
   for(int i = 0; i < total; i++)
     {
      string candidate = SymbolName(i, false);
      string normalized = AlgentraNormalizeSymbol(candidate);
      if((StringFind(normalized, "XAU") >= 0 || StringFind(normalized, "GOLD") >= 0) && nearby_count < 8)
        {
         nearby += (nearby_count == 0 ? "" : ", ") + candidate;
         nearby_count++;
        }
      int suffix_length = 0;
      // First pass only accepts XAUUSD-prefixed spot pairs. GOLD aliases are
      // considered in a second pass, only when no valid prefix was found.
      if(!AlgentraCandidateIsGoldSpot(candidate, normalized, false, suffix_length))
         continue;
      int size = ArraySize(candidates);
      ArrayResize(candidates, size + 1);
      candidates[size].name = candidate;
      long trade_mode = SymbolInfoInteger(candidate, SYMBOL_TRADE_MODE);
      bool visible = SymbolInfoInteger(candidate, SYMBOL_VISIBLE) != 0;
      candidates[size].preferred = (visible && trade_mode == SYMBOL_TRADE_MODE_FULL) ? 1 : 0;
      candidates[size].suffix_length = suffix_length;
      candidates[size].selected = SymbolInfoInteger(candidate, SYMBOL_SELECT) != 0 ? 1 : 0;
      candidates[size].xauusd_prefix = 1;
     }

   if(ArraySize(candidates) == 0)
     {
      for(int i = 0; i < total; i++)
        {
         string candidate = SymbolName(i, false);
         string normalized = AlgentraNormalizeSymbol(candidate);
         int suffix_length = 0;
         if(!AlgentraCandidateIsGoldSpot(candidate, normalized, true, suffix_length) ||
            StringFind(normalized, "GOLD") != 0)
            continue;
         int size = ArraySize(candidates);
         ArrayResize(candidates, size + 1);
         candidates[size].name = candidate;
         long trade_mode = SymbolInfoInteger(candidate, SYMBOL_TRADE_MODE);
         bool visible = SymbolInfoInteger(candidate, SYMBOL_VISIBLE) != 0;
         candidates[size].preferred = (visible && trade_mode == SYMBOL_TRADE_MODE_FULL) ? 1 : 0;
         candidates[size].suffix_length = suffix_length;
         candidates[size].selected = SymbolInfoInteger(candidate, SYMBOL_SELECT) != 0 ? 1 : 0;
         candidates[size].xauusd_prefix = 0;
        }
     }

   if(ArraySize(candidates) == 0)
     {
      reason = "simbol XAUUSD belum tersedia; kandidat terdekat: " + (nearby == "" ? "tidak ada" : nearby);
      if(now - g_algentra_gold_last_log >= 15)
        {
         Print("[GoldSymbolResolver] ", reason, "; broker=", server);
         g_algentra_gold_last_log = now;
        }
      return false;
     }

   int best = 0;
   for(int i = 1; i < ArraySize(candidates); i++)
      if(AlgentraCandidateRanksBefore(candidates[i], candidates[best]))
         best = i;

   string chosen = candidates[best].name;
   ResetLastError();
   if(!SymbolSelect(chosen, true))
     {
      reason = "gagal mengaktifkan simbol " + chosen + " di Market Watch; error " + IntegerToString(GetLastError());
      if(now - g_algentra_gold_last_log >= 15)
        {
         Print("[GoldSymbolResolver] ", reason, "; broker=", server,
               "; kandidat=[", nearby, "]");
         g_algentra_gold_last_log = now;
        }
      return false;
     }
   g_algentra_gold_symbol = chosen;
   resolved = chosen;
   reason = "simbol emas XAUUSD terpilih: " + chosen;
   Print("[GoldSymbolResolver] broker=", server, " simbol=", chosen);
   return true;
  }

#endif
