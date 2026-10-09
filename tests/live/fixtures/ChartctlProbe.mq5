//+------------------------------------------------------------------+
//| ChartctlProbe.mq5                                                |
//| Live-suite expert for Chart Deployments. It never trades: it     |
//| logs to the Experts journal, draws what it sees on the chart,    |
//| and writes the chart comment on every timer tick so the suite    |
//| can check that the loader's ownership survives an expert that    |
//| calls Comment().                                                 |
//+------------------------------------------------------------------+
#property version "1.00"
#property description "Chart Deployments live-test probe. Logs and draws, never trades."

input string ProbeLabel   = "no-set";
input int    TimerSeconds = 2;
input color  LabelColor   = clrLime;

const string OBJ_PREFIX = "chartctl_probe_";

int      g_ticks   = 0;
datetime g_started = 0;

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
                                  (int)(TimeCurrent() - g_started)), 95, 14);
   SetLabel("line3", StringFormat("account %I64d  %s", AccountInfoInteger(ACCOUNT_LOGIN),
                                  TradeModeName()), 120, 14);
   Comment("ChartctlProbe label=" + ProbeLabel + " "
           + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS));
   ChartRedraw(0);
}

int OnInit()
{
   g_started = TimeCurrent();
   EventSetTimer(MathMax(1, TimerSeconds));
   PrintFormat("[ChartctlProbe] init label=%s symbol=%s tf=%s account=%I64d mode=%s",
               ProbeLabel, _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
               AccountInfoInteger(ACCOUNT_LOGIN), TradeModeName());
   Draw();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
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
}
