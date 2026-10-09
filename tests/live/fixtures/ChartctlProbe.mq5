//+------------------------------------------------------------------+
//| ChartctlProbe.mq5                                                |
//| Live-suite expert for Chart Deployments. It never trades: it     |
//| logs to the Experts journal, draws what it sees on the chart,    |
//| and writes the chart comment on every timer tick so the suite    |
//| can check that the loader's ownership survives an expert that    |
//| calls Comment().                                                 |
//|                                                                  |
//| Optional, off unless the set file names them:                    |
//|   LockFile      held open without sharing for the expert's life, |
//|                 so the file API's FILE_LOCKED path can be hit.   |
//|   WebRequestUrl requested on every timer tick; the HTTP code and |
//|                 GetLastError() go to StatusFile, which shows the |
//|                 suite whether the WebRequest allowlist is live.  |
//| Paths are relative to MQL5\Files.                                |
//+------------------------------------------------------------------+
#property version "1.10"
#property description "Chart Deployments live-test probe. Logs and draws, never trades."

input string ProbeLabel    = "no-set";
input int    TimerSeconds  = 2;
input color  LabelColor    = clrLime;
input string LockFile      = "";
input string WebRequestUrl = "";
input string StatusFile    = "";

const string OBJ_PREFIX = "chartctl_probe_";
const int    WEBREQUEST_TIMEOUT_MS = 5000;

int      g_ticks   = 0;
datetime g_started = 0;
int      g_lock    = INVALID_HANDLE;

string TradeModeName()
{
   switch((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE))
   {
      case ACCOUNT_TRADE_MODE_DEMO:    return "DEMO";
      case ACCOUNT_TRADE_MODE_CONTEST: return "CONTEST";
      default:                         return "REAL";
   }
}

void SetLabel(const string name, const string text, const int y, const int size)
{
   string id = OBJ_PREFIX + name;
   if(ObjectFind(0, id) < 0)
   {
      ObjectCreate(0, id, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, id, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
      ObjectSetInteger(0, id, OBJPROP_ANCHOR, ANCHOR_RIGHT_UPPER);
      ObjectSetInteger(0, id, OBJPROP_XDISTANCE, 20);
      ObjectSetInteger(0, id, OBJPROP_SELECTABLE, false);
   }
   ObjectSetInteger(0, id, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, id, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, id, OBJPROP_COLOR, LabelColor);
   ObjectSetString(0, id, OBJPROP_TEXT, text);
}

void Draw()
{
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   string tf  = StringSubstr(EnumToString((ENUM_TIMEFRAMES)_Period), 7);
   SetLabel("title", "CHARTCTL PROBE: " + ProbeLabel, 30, 22);
   SetLabel("line1", StringFormat("%s %s  bid %s", _Symbol, tf,
                                  DoubleToString(bid, _Digits)), 70, 14);
   SetLabel("line2", StringFormat("ticks %d  up %ds", g_ticks,
                                  (int)(TimeLocal() - g_started)), 95, 14);
   SetLabel("line3", StringFormat("account %I64d  %s", AccountInfoInteger(ACCOUNT_LOGIN),
                                  TradeModeName()), 120, 14);
   Comment("ChartctlProbe label=" + ProbeLabel + " "
           + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS));
   ChartRedraw(0);
}

void HoldLockFile()
{
   if(LockFile == "")
      return;
   // No FILE_SHARE_* flags: Windows then refuses every other open, replace
   // and delete of the file while this handle lives.
   g_lock = FileOpen(LockFile, FILE_WRITE | FILE_BIN);
   if(g_lock == INVALID_HANDLE)
   {
      PrintFormat("[ChartctlProbe] could not hold %s, error %d", LockFile, GetLastError());
      return;
   }
   FileWriteString(g_lock, "held by ChartctlProbe");
   FileFlush(g_lock);
}

void ReportWebRequest()
{
   if(WebRequestUrl == "" || StatusFile == "")
      return;
   char   body[];
   char   reply[];
   string reply_headers;
   ResetLastError();
   int code  = WebRequest("GET", WebRequestUrl, "", WEBREQUEST_TIMEOUT_MS, body, reply, reply_headers);
   int error = GetLastError();
   int status = FileOpen(StatusFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(status == INVALID_HANDLE)
      return;
   FileWriteString(status, StringFormat("started=%d\ncode=%d\nerror=%d\nat=%d\n",
                                        (long)g_started, code, error, (long)TimeLocal()));
   FileClose(status);
}

int OnInit()
{
   g_started = TimeLocal();
   EventSetTimer(MathMax(1, TimerSeconds));
   PrintFormat("[ChartctlProbe] init label=%s symbol=%s tf=%s account=%I64d mode=%s",
               ProbeLabel, _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
               AccountInfoInteger(ACCOUNT_LOGIN), TradeModeName());
   HoldLockFile();
   Draw();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_lock != INVALID_HANDLE)
      FileClose(g_lock);
   ObjectsDeleteAll(0, OBJ_PREFIX);
   Comment("");
   PrintFormat("[ChartctlProbe] deinit label=%s reason=%d ticks=%d", ProbeLabel, reason, g_ticks);
}

void OnTick()
{
   g_ticks++;
}

void OnTimer()
{
   Draw();
   ReportWebRequest();
}
