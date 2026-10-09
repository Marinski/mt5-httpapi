//+------------------------------------------------------------------+
//|                                               ChartControl.mqh    |
//|  Chart Control Protocol v1 — reference implementation             |
//|                                                                   |
//|  Drop this into a resident EA to make it the terminal's chart     |
//|  deployment loader. It reconciles the terminal's charts to the    |
//|  desired state written by mt5-httpapi's chartctl endpoints.       |
//|                                                                   |
//|  Usage inside your EA:                                            |
//|    #include <ChartControl.mqh>                                    |
//|    CChartControl ctl;                                             |
//|    int OnInit(){ if(!ctl.Init()) return INIT_FAILED;              |
//|                  EventSetTimer(1); return INIT_SUCCEEDED; }       |
//|    void OnTimer(){ ctl.Tick(); }                                  |
//|    void OnDeinit(const int r){ ctl.Deinit(); }                    |
//|                                                                   |
//|  Contract & file formats: docs/chart-control-protocol.md          |
//+------------------------------------------------------------------+
#property strict

#define CHARTCTL_PROTOCOL   1
#define CHARTCTL_VERSION    "1.0.3"
#define CHARTCTL_DIR        "chartctl"          // under MQL5\Files\
#define CHARTCTL_OWNED_FILE "owned.json"        // chart_id -> deployment_id
// Experts on the charts MT5 restores at startup load a few seconds after the
// loader does. Until this much time has passed the loader only adopts and
// opens no charts, or it would duplicate a chart whose expert is still loading.
#define CHARTCTL_STARTUP_GRACE_MS 30000
#define CHARTCTL_MUTEX_GV   "chartctl_loader_owner"
#define CHARTCTL_ID_INPUT   "__chartctl_id"

//--- one desired deployment
struct ChartCtlDeployment
{
   string id;
   string expert;      // expert short name (CHART_EXPERT_NAME match target)
   string templ;       // template path for ChartApplyTemplate; leading \ = relative to <data>\MQL5 (e.g. \Files\chartctl\dep_x.tpl)
   string symbol;
   string timeframe;
   bool   enabled;
};

//--- what we observed on one chart
struct ChartCtlChart
{
   long   chart_id;
   string symbol;
   string timeframe;
   string expert;
   bool   expert_enabled;
   string deployment_id;   // from the ownership map; "" when no deployment owns it
};

//+------------------------------------------------------------------+
class CChartControl
{
private:
   bool     m_owner;              // did we win the single-loader mutex?
   ulong    m_owner_since_ms;     // GetTickCount64() when we became owner
   long     m_applied_revision;   // last desired revision we reconciled
   long     m_last_revision_seen;
   datetime m_started;
   // Per-deployment error slots (parallel arrays keyed by deployment id).
   // A single shared slot let one deployment's error mask another's.
   string   m_err_ids[];
   string   m_err_codes[];
   string   m_err_details[];
   datetime m_err_times[];        // drives the failed-attach retry cooldown
   // Which deployment owns which chart (parallel arrays), persisted to
   // CHARTCTL_OWNED_FILE. Kept here rather than in the chart comment
   // because any expert that calls Comment() overwrites the comment.
   long     m_own_charts[];
   string   m_own_ids[];
   bool     m_own_dirty;          // map changed but not yet written to disk

   //--- ownership map ------------------------------------------------
   void     LoadOwnership(void);
   void     SaveOwnership(void);
   string   OwnerOf(const long chart_id);
   void     Claim(const long chart_id, const string dep_id);
   void     Release(const long chart_id);
   void     PruneOwnership(ChartCtlChart &charts[]);

   //--- file helpers -------------------------------------------------
   bool     ReadFile(const string relpath, string &out);
   bool     WriteFileAtomic(const string relpath, const string content);
   void     DeleteFileSafe(const string relpath);

   //--- json (minimal, tailored to our own compact output) ----------
   string   JsonStr(const string key, const string s, const string json);
   long     JsonNum(const string key, const string json);
   bool     ExtractDeployments(const string json, ChartCtlDeployment &out[]);
   string   JsonEscape(const string s);

   //--- reconcile ----------------------------------------------------
   void     ScanCharts(ChartCtlChart &out[]);
   long     FindChartFor(const string dep_id, ChartCtlChart &charts[]);
   long     FindAdoptableChart(const ChartCtlDeployment &dep, ChartCtlChart &charts[]);
   bool     AttachDeployment(const ChartCtlDeployment &dep);
   void     DetachChart(const long chart_id);
   ENUM_TIMEFRAMES TF(const string s);
   void     RecordError(const string id, const string code, const string detail);
   void     ClearError(const string id);
   bool     InRetryCooldown(const string id);

   //--- observed + command output -----------------------------------
   void     WriteObserved(ChartCtlDeployment &desired[], ChartCtlChart &charts[]);
   void     HandleCommand();

public:
            CChartControl(void);
   // close_own_chart_on_duplicate: standalone loaders pass true so a
   // second copy (e.g. re-fired by mt5start.ini [StartUp] on every
   // launch) closes its own chart and vanishes instead of idling.
   // EAs that embed this module MUST leave it false — closing the chart
   // would kill the host EA (e.g. an account tracker) too.
   bool     Init(const bool close_own_chart_on_duplicate=false);
   void     Tick(void);
   void     Deinit(void);
   bool     IsOwner(void) const { return m_owner; }
};

//+------------------------------------------------------------------+
CChartControl::CChartControl(void)
{
   m_owner = false;
   m_owner_since_ms = 0;
   m_applied_revision = -1;
   m_last_revision_seen = -1;
   m_started = 0;
   m_own_dirty = false;
}

//+------------------------------------------------------------------+
//| Claim the single-loader mutex via a terminal GlobalVariable.     |
//+------------------------------------------------------------------+
bool CChartControl::Init(const bool close_own_chart_on_duplicate)
{
   m_started = TimeCurrent();

   // If another loader already holds the mutex and is fresh, step aside.
   if(GlobalVariableCheck(CHARTCTL_MUTEX_GV))
   {
      datetime held = (datetime)GlobalVariableGet(CHARTCTL_MUTEX_GV);
      // Treat a mutex touched within 120s as a live owner.
      if(TimeCurrent() - held < 120)
      {
         m_owner = false;
         if(close_own_chart_on_duplicate)
         {
            // Standalone duplicate (e.g. [StartUp] re-fired on relaunch):
            // remove ourselves entirely so charts never accumulate.
            Print("ChartControl: live owner exists; closing own chart.");
            ChartClose(ChartID());
            return true;   // unloading anyway; don't fail the host
         }
         Print("ChartControl: another loader owns the mutex; standing down.");
         return true;   // do NOT fail the host EA — just stay passive
      }
   }
   GlobalVariableSet(CHARTCTL_MUTEX_GV, (double)TimeCurrent());
   GlobalVariableTemp(CHARTCTL_MUTEX_GV);   // auto-clears if terminal exits
   m_owner = true;
   m_owner_since_ms = GetTickCount64();
   LoadOwnership();
   PrintFormat("ChartControl v%s active (owner). dir=MQL5\\Files\\%s",
               CHARTCTL_VERSION, CHARTCTL_DIR);
   return true;
}

//+------------------------------------------------------------------+
void CChartControl::Tick(void)
{
   if(!m_owner)
   {
      // Passive mode: try to reclaim if the previous owner is gone.
      if(!GlobalVariableCheck(CHARTCTL_MUTEX_GV))
         Init();
      return;
   }

   // Refresh mutex heartbeat.
   GlobalVariableSet(CHARTCTL_MUTEX_GV, (double)TimeCurrent());

   // Always answer commands (screenshot etc.), even without desired change.
   HandleCommand();

   string desired_json;
   ChartCtlDeployment desired[];
   if(ReadFile(CHARTCTL_DIR + "\\desired.json", desired_json))
   {
      long rev = JsonNum("revision", desired_json);
      ExtractDeployments(desired_json, desired);

      // Reconcile every pass (cheap) — attach missing, detach orphans.
      ChartCtlChart charts[];
      ScanCharts(charts);

      // 1) Attach / repair enabled deployments.
      for(int i = 0; i < ArraySize(desired); i++)
      {
         if(!desired[i].enabled)
            continue;
         long cid = FindChartFor(desired[i].id, charts);
         if(cid >= 0)
            continue;
         // Adopt before opening: an unowned chart already running this
         // exact expert/symbol/timeframe is almost certainly a previous
         // incarnation of this deployment, such as one MT5 restored under a
         // new chart id after a terminal restart. Claiming it instead of
         // opening a fresh chart is what stops duplicates accumulating.
         cid = FindAdoptableChart(desired[i], charts);
         if(cid >= 0)
         {
            Claim(cid, desired[i].id);
            ClearError(desired[i].id);
            PrintFormat("ChartControl: adopted chart %I64d for %s (%s %s)",
                        cid, desired[i].id, desired[i].symbol,
                        desired[i].timeframe);
            continue;
         }
         if(InRetryCooldown(desired[i].id))
            continue;   // recent failure — don't hammer ChartOpen every pass
         if(GetTickCount64() - m_owner_since_ms < CHARTCTL_STARTUP_GRACE_MS)
            continue;   // a restored chart may still be loading this expert
         AttachDeployment(desired[i]);
      }

      // 2) Detach charts we own whose deployment is gone or disabled.
      ScanCharts(charts);   // rescan after possible attaches
      for(int c = 0; c < ArraySize(charts); c++)
      {
         if(charts[c].deployment_id == "")
            continue;   // not ours — never touch
         bool wanted = false;
         for(int d = 0; d < ArraySize(desired); d++)
            if(desired[d].id == charts[c].deployment_id && desired[d].enabled)
            { wanted = true; break; }
         if(!wanted)
            DetachChart(charts[c].chart_id);
      }

      m_applied_revision = rev;
      ScanCharts(charts);
      PruneOwnership(charts);
      WriteObserved(desired, charts);
   }
   else
   {
      // No desired file yet — still publish liveness + inventory.
      ChartCtlChart charts[];
      ScanCharts(charts);
      PruneOwnership(charts);
      ChartCtlDeployment none[];
      WriteObserved(none, charts);
   }

   if(m_own_dirty)
      SaveOwnership();   // retry a write that failed earlier
}

//+------------------------------------------------------------------+
void CChartControl::Deinit(void)
{
   if(m_owner && GlobalVariableCheck(CHARTCTL_MUTEX_GV))
      GlobalVariableDel(CHARTCTL_MUTEX_GV);
}

//+------------------------------------------------------------------+
//| Attach: select symbol, open chart, apply template, verify.       |
//+------------------------------------------------------------------+
bool CChartControl::AttachDeployment(const ChartCtlDeployment &dep)
{
   if(!SymbolSelect(dep.symbol, true))
   {
      RecordError(dep.id, "SYMBOL_NOT_FOUND",
                  "SymbolSelect failed for " + dep.symbol);
      return false;
   }

   long cid = ChartOpen(dep.symbol, TF(dep.timeframe));
   if(cid == 0)
   {
      RecordError(dep.id, "CHART_OPEN_FAILED",
                  "ChartOpen failed err=" + IntegerToString(GetLastError()));
      return false;
   }

   if(!ChartApplyTemplate(cid, dep.templ))
   {
      RecordError(dep.id, "TEMPLATE_APPLY_FAILED",
                  "ChartApplyTemplate(" + dep.templ + ") err="
                  + IntegerToString(GetLastError()));
      ChartClose(cid);
      return false;
   }

   // Verify the expert actually attached within ~10s.
   for(int i = 0; i < 40; i++)
   {
      ChartRedraw(cid);
      // CHART_EXPERT_NAME is NULL (not "") when no expert is attached,
      // and NULL != "" is true in MQL5 — test length, or the very first
      // iteration false-passes and an expert-less chart reports running.
      string en = ChartGetString(cid, CHART_EXPERT_NAME);
      if(StringLen(en) > 0)
      {
         Claim(cid, dep.id);
         ClearError(dep.id);
         PrintFormat("ChartControl: attached %s on %s %s (chart %I64d)",
                     en, dep.symbol, dep.timeframe, cid);
         return true;
      }
      Sleep(250);
   }

   // Leaving the chart open here leaks an expert-less chart per pass (the
   // expert may still load later, but then adoption reclaims a closed-and-
   // reopened one just as well). Close what we opened.
   ChartClose(cid);
   RecordError(dep.id, "EXPERT_NOT_ATTACHED",
               "template applied but CHART_EXPERT_NAME empty after 10s "
               "(an .ex5 copied in while the terminal runs is not loadable "
               "until the Navigator is refreshed); GetLastError="
               + IntegerToString(GetLastError()));
   return false;
}

//+------------------------------------------------------------------+
//| Ownership map: which deployment owns which open chart.           |
//+------------------------------------------------------------------+
string CChartControl::OwnerOf(const long chart_id)
{
   for(int i = 0; i < ArraySize(m_own_charts); i++)
      if(m_own_charts[i] == chart_id)
         return m_own_ids[i];
   return "";
}

// One chart per deployment: claiming drops any older chart the deployment
// held, and replaces any other owner of this chart.
void CChartControl::Claim(const long chart_id, const string dep_id)
{
   for(int i = ArraySize(m_own_charts) - 1; i >= 0; i--)
      if(m_own_charts[i] == chart_id || m_own_ids[i] == dep_id)
         Release(m_own_charts[i]);
   int n = ArraySize(m_own_charts);
   ArrayResize(m_own_charts, n + 1);
   ArrayResize(m_own_ids, n + 1);
   m_own_charts[n] = chart_id;
   m_own_ids[n] = dep_id;
   m_own_dirty = true;
   SaveOwnership();
}

void CChartControl::Release(const long chart_id)
{
   for(int i = 0; i < ArraySize(m_own_charts); i++)
   {
      if(m_own_charts[i] != chart_id)
         continue;
      int last = ArraySize(m_own_charts) - 1;
      m_own_charts[i] = m_own_charts[last];
      m_own_ids[i] = m_own_ids[last];
      ArrayResize(m_own_charts, last);
      ArrayResize(m_own_ids, last);
      m_own_dirty = true;
      return;
   }
}

// Drop entries for charts that no longer exist: closed by hand, or given a
// new id by a terminal restart.
void CChartControl::PruneOwnership(ChartCtlChart &charts[])
{
   for(int i = ArraySize(m_own_charts) - 1; i >= 0; i--)
   {
      bool open = false;
      for(int c = 0; c < ArraySize(charts); c++)
         if(charts[c].chart_id == m_own_charts[i]) { open = true; break; }
      if(!open)
         Release(m_own_charts[i]);
   }
   if(m_own_dirty)
      SaveOwnership();
}

void CChartControl::SaveOwnership(void)
{
   string j = "{\"owned\":[";
   for(int i = 0; i < ArraySize(m_own_charts); i++)
   {
      if(i) j += ",";
      j += "{\"chart_id\":" + IntegerToString(m_own_charts[i])
         + ",\"deployment_id\":\"" + JsonEscape(m_own_ids[i]) + "\"}";
   }
   j += "]}";
   // On failure the map stays dirty and Tick retries; the in-memory map
   // stays authoritative meanwhile.
   if(WriteFileAtomic(CHARTCTL_DIR + "\\" + CHARTCTL_OWNED_FILE, j))
      m_own_dirty = false;
}

void CChartControl::LoadOwnership(void)
{
   ArrayResize(m_own_charts, 0);
   ArrayResize(m_own_ids, 0);
   string json;
   if(!ReadFile(CHARTCTL_DIR + "\\" + CHARTCTL_OWNED_FILE, json))
      return;
   int depth = 0, obj_start = -1;
   for(int i = 0; i < StringLen(json); i++)
   {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '{')
      {
         depth++;
         if(depth == 2) obj_start = i;
      }
      else if(ch == '}')
      {
         if(depth == 2 && obj_start >= 0)
         {
            string obj = StringSubstr(json, obj_start, i - obj_start + 1);
            long cid = JsonNum("chart_id", obj);
            string dep = JsonStr("deployment_id", "", obj);
            if(cid != 0 && dep != "")
            {
               int n = ArraySize(m_own_charts);
               ArrayResize(m_own_charts, n + 1);
               ArrayResize(m_own_ids, n + 1);
               m_own_charts[n] = cid;
               m_own_ids[n] = dep;
            }
            obj_start = -1;
         }
         depth--;
      }
   }
   PrintFormat("ChartControl: loaded %d owned chart(s) from %s",
               ArraySize(m_own_charts), CHARTCTL_OWNED_FILE);
}

//+------------------------------------------------------------------+
//| An unowned chart matching a deployment's expert+symbol+timeframe |
//| (a prior incarnation from before a terminal restart, or a        |
//| verify-timeout chart whose expert loaded late).                  |
//+------------------------------------------------------------------+
long CChartControl::FindAdoptableChart(const ChartCtlDeployment &dep,
                                       ChartCtlChart &charts[])
{
   for(int i = 0; i < ArraySize(charts); i++)
   {
      if(charts[i].deployment_id != "")
         continue;   // owned by another deployment
      if(!charts[i].expert_enabled)
         continue;
      if(charts[i].expert != dep.expert)
         continue;
      if(charts[i].symbol != dep.symbol)
         continue;
      if(charts[i].timeframe != "PERIOD_" + dep.timeframe)
         continue;
      return charts[i].chart_id;
   }
   return -1;
}

//+------------------------------------------------------------------+
void CChartControl::DetachChart(const long chart_id)
{
   PrintFormat("ChartControl: detaching chart %I64d", chart_id);
   // Kept in the map on failure: the chart and its expert are still there,
   // and the next pass tries again.
   if(ChartClose(chart_id))
      Release(chart_id);
   else
      PrintFormat("ChartControl: ChartClose(%I64d) failed (err=%d)",
                  chart_id, GetLastError());
}

//+------------------------------------------------------------------+
//| Enumerate all open charts and attribute each to its deployment   |
//| through the ownership map.                                       |
//+------------------------------------------------------------------+
void CChartControl::ScanCharts(ChartCtlChart &out[])
{
   ArrayResize(out, 0);
   long cid = ChartFirst();
   int guard = 0;
   while(cid >= 0 && guard < 1000)
   {
      guard++;
      ChartCtlChart c;
      c.chart_id       = cid;
      c.symbol         = ChartSymbol(cid);
      c.timeframe      = EnumToString(ChartPeriod(cid));
      c.expert         = ChartGetString(cid, CHART_EXPERT_NAME);
      c.expert_enabled = (c.expert != "");
      // We cannot read a foreign expert's inputs from MQL5, so the
      // template's __chartctl_id input cannot mark the chart; the
      // ownership map does.
      c.deployment_id  = OwnerOf(cid);

      int n = ArraySize(out);
      ArrayResize(out, n + 1);
      out[n] = c;

      cid = ChartNext(cid);
   }
}

//+------------------------------------------------------------------+
long CChartControl::FindChartFor(const string dep_id, ChartCtlChart &charts[])
{
   for(int i = 0; i < ArraySize(charts); i++)
      if(charts[i].deployment_id == dep_id && charts[i].expert_enabled)
         return charts[i].chart_id;
   return -1;
}

//+------------------------------------------------------------------+
//| Command channel: one-shot ops that produce artifacts.            |
//+------------------------------------------------------------------+
void CChartControl::HandleCommand(void)
{
   string body;
   if(!ReadFile(CHARTCTL_DIR + "\\command.json", body))
      return;

   string cmd_id = JsonStr("command_id", "", body);
   string action = JsonStr("action", "", body);
   if(cmd_id == "")
   {
      DeleteFileSafe(CHARTCTL_DIR + "\\command.json");
      return;
   }

   string result = "{";
   result += "\"command_id\":\"" + JsonEscape(cmd_id) + "\",";

   if(action == "screenshot")
   {
      long cid = JsonNum("chart_id", body);
      int  w   = (int)JsonNum("width", body);  if(w <= 0) w = 1280;
      int  h   = (int)JsonNum("height", body); if(h <= 0) h = 720;
      string fname = "shots\\" + cmd_id + ".png";
      // ChartScreenShot writes under MQL5\Files\.
      if(ChartScreenShot((long)cid, CHARTCTL_DIR + "\\" + fname, w, h))
         result += "\"status\":\"ok\",\"file\":\"" + cmd_id + ".png\"";
      else
         result += "\"status\":\"error\",\"error_code\":\"SCREENSHOT_FAILED\","
                 + "\"error_detail\":\"err="
                 + IntegerToString(GetLastError()) + "\"";
   }
   else if(action == "reconcile")
   {
      m_applied_revision = -1;   // force a full reconcile next pass
      result += "\"status\":\"ok\"";
   }
   else if(action == "close_chart")
   {
      long cid = JsonNum("chart_id", body);
      if(cid == ChartID())
         result += "\"status\":\"error\",\"error_code\":\"CLOSE_REFUSED\","
                 + "\"error_detail\":\"refusing to close the loader's own chart\"";
      else if(ChartClose(cid))
         result += "\"status\":\"ok\"";
      else
         result += "\"status\":\"error\",\"error_code\":\"CLOSE_FAILED\","
                 + "\"error_detail\":\"err="
                 + IntegerToString(GetLastError()) + "\"";
   }
   else
   {
      result += "\"status\":\"error\",\"error_code\":\"UNKNOWN_ACTION\","
              + "\"error_detail\":\"" + JsonEscape(action) + "\"";
   }
   result += "}";

   WriteFileAtomic(CHARTCTL_DIR + "\\command_result.json", result);
   DeleteFileSafe(CHARTCTL_DIR + "\\command.json");
}

//+------------------------------------------------------------------+
//| Write observed.json — the API's window into terminal truth.      |
//+------------------------------------------------------------------+
void CChartControl::WriteObserved(ChartCtlDeployment &desired[],
                                  ChartCtlChart &charts[])
{
   string j = "{";
   j += "\"protocol\":" + IntegerToString(CHARTCTL_PROTOCOL) + ",";
   j += "\"loader\":{";
   j +=   "\"name\":\"" + JsonEscape(MQLInfoString(MQL_PROGRAM_NAME)) + "\",";
   j +=   "\"version\":\"" + CHARTCTL_VERSION + "\",";
   j +=   "\"last_loop\":\"" + TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS) + "\",";
   j +=   "\"applied_revision\":" + IntegerToString(m_applied_revision);
   j += "},";
   j += "\"terminal\":{\"auto_trading\":"
        + (string)(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false")
        + "},";

   // charts[]
   j += "\"charts\":[";
   for(int i = 0; i < ArraySize(charts); i++)
   {
      if(i) j += ",";
      j += "{";
      j += "\"chart_id\":" + IntegerToString(charts[i].chart_id) + ",";
      j += "\"symbol\":\"" + JsonEscape(charts[i].symbol) + "\",";
      j += "\"timeframe\":\"" + JsonEscape(charts[i].timeframe) + "\",";
      j += "\"expert\":\"" + JsonEscape(charts[i].expert) + "\",";
      j += "\"expert_enabled\":" + (string)(charts[i].expert_enabled ? "true" : "false") + ",";
      j += "\"deployment_id\":\"" + JsonEscape(charts[i].deployment_id) + "\"";
      j += "}";
   }
   j += "],";

   // deployments[] status
   j += "\"deployments\":[";
   int written = 0;
   for(int d = 0; d < ArraySize(desired); d++)
   {
      long cid = FindChartFor(desired[d].id, charts);
      string status = (cid >= 0) ? "running"
                    : (desired[d].enabled ? "pending" : "paused");
      if(written) j += ",";
      j += "{\"id\":\"" + JsonEscape(desired[d].id) + "\",";
      j += "\"status\":\"" + status + "\"";
      if(cid >= 0) j += ",\"chart_id\":" + IntegerToString(cid);
      j += "}";
      written++;
   }
   j += "],";

   // errors[] — one entry per failing deployment, cleared on its success
   j += "\"errors\":[";
   for(int e = 0; e < ArraySize(m_err_ids); e++)
   {
      if(e) j += ",";
      j += "{\"id\":\"" + JsonEscape(m_err_ids[e]) + "\",";
      j += "\"status\":\"failed\",";
      j += "\"code\":\"" + JsonEscape(m_err_codes[e]) + "\",";
      j += "\"detail\":\"" + JsonEscape(m_err_details[e]) + "\"}";
   }
   j += "]";

   j += "}";
   WriteFileAtomic(CHARTCTL_DIR + "\\observed.json", j);
}

//+------------------------------------------------------------------+
void CChartControl::RecordError(const string id, const string code,
                                const string detail)
{
   int slot = -1;
   for(int i = 0; i < ArraySize(m_err_ids); i++)
      if(m_err_ids[i] == id) { slot = i; break; }
   if(slot < 0)
   {
      slot = ArraySize(m_err_ids);
      ArrayResize(m_err_ids, slot + 1);
      ArrayResize(m_err_codes, slot + 1);
      ArrayResize(m_err_details, slot + 1);
      ArrayResize(m_err_times, slot + 1);
   }
   m_err_ids[slot] = id;
   m_err_codes[slot] = code;
   m_err_details[slot] = detail;
   m_err_times[slot] = TimeCurrent();
   PrintFormat("ChartControl ERROR [%s] %s: %s", id, code, detail);
}

void CChartControl::ClearError(const string id)
{
   for(int i = 0; i < ArraySize(m_err_ids); i++)
   {
      if(m_err_ids[i] != id)
         continue;
      int last = ArraySize(m_err_ids) - 1;
      m_err_ids[i] = m_err_ids[last];
      m_err_codes[i] = m_err_codes[last];
      m_err_details[i] = m_err_details[last];
      m_err_times[i] = m_err_times[last];
      ArrayResize(m_err_ids, last);
      ArrayResize(m_err_codes, last);
      ArrayResize(m_err_details, last);
      ArrayResize(m_err_times, last);
      return;
   }
}

// A deployment that just failed to attach gets a 60s cooldown so the
// loader doesn't churn ChartOpen/ChartClose on every reconcile pass.
bool CChartControl::InRetryCooldown(const string id)
{
   for(int i = 0; i < ArraySize(m_err_ids); i++)
      if(m_err_ids[i] == id)
         return (TimeCurrent() - m_err_times[i]) < 60;
   return false;
}

//+------------------------------------------------------------------+
//| File helpers — everything under MQL5\Files\ (FILE_COMMON off).   |
//+------------------------------------------------------------------+
bool CChartControl::ReadFile(const string relpath, string &out)
{
   int h = FileOpen(relpath, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return false;
   out = "";
   while(!FileIsEnding(h))
      out += FileReadString(h);
   FileClose(h);
   return true;
}

bool CChartControl::WriteFileAtomic(const string relpath, const string content)
{
   string tmp = relpath + ".tmp";
   int h = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
   {
      PrintFormat("ChartControl: cannot open %s for write (err=%d)",
                  tmp, GetLastError());
      return false;
   }
   FileWriteString(h, content);
   FileClose(h);
   // FileMove with rewrite flag = atomic-ish replace on Windows. Without
   // FILE_REWRITE the move needs the pre-delete to have worked, and that
   // fails with 5020 whenever the API side has the file open for a read.
   if(!FileMove(tmp, 0, relpath, FILE_REWRITE))
   {
      PrintFormat("ChartControl: FileMove %s->%s failed (err=%d)",
                  tmp, relpath, GetLastError());
      return false;
   }
   return true;
}

void CChartControl::DeleteFileSafe(const string relpath)
{
   if(FileIsExist(relpath))
      FileDelete(relpath);
}

//+------------------------------------------------------------------+
//| Minimal JSON readers for our own compact, predictable output.    |
//| NOT a general parser — only the shapes chartctl produces.        |
//+------------------------------------------------------------------+
string CChartControl::JsonStr(const string key, const string def,
                              const string json)
{
   string needle = "\"" + key + "\"";
   int p = StringFind(json, needle);
   if(p < 0) return def;
   int colon = StringFind(json, ":", p + StringLen(needle));
   if(colon < 0) return def;
   int q1 = StringFind(json, "\"", colon + 1);
   if(q1 < 0) return def;
   // find unescaped closing quote
   int i = q1 + 1;
   string val = "";
   while(i < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '\\')
      {
         if(i + 1 < StringLen(json))
            val += ShortToString(StringGetCharacter(json, i + 1));
         i += 2;
         continue;
      }
      if(ch == '"')
         break;
      val += ShortToString(ch);
      i++;
   }
   return val;
}

long CChartControl::JsonNum(const string key, const string json)
{
   string needle = "\"" + key + "\"";
   int p = StringFind(json, needle);
   if(p < 0) return 0;
   int colon = StringFind(json, ":", p + StringLen(needle));
   if(colon < 0) return 0;
   int i = colon + 1;
   string num = "";
   while(i < StringLen(json))
   {
      ushort ch = StringGetCharacter(json, i);
      if((ch >= '0' && ch <= '9') || ch == '-')
         num += ShortToString(ch);
      else if(num != "")
         break;
      i++;
   }
   return (long)StringToInteger(num);
}

//+------------------------------------------------------------------+
//| Extract the deployments[] array from desired.json.               |
//| Each object: {id,expert,template,symbol,timeframe,enabled}.      |
//+------------------------------------------------------------------+
bool CChartControl::ExtractDeployments(const string json,
                                       ChartCtlDeployment &out[])
{
   ArrayResize(out, 0);
   int arr = StringFind(json, "\"deployments\"");
   if(arr < 0) return false;
   int i = StringFind(json, "[", arr);
   if(i < 0) return false;

   int depth = 0;
   int obj_start = -1;
   for(; i < StringLen(json); i++)
   {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '{')
      {
         if(depth == 0) obj_start = i;
         depth++;
      }
      else if(ch == '}')
      {
         depth--;
         if(depth == 0 && obj_start >= 0)
         {
            string obj = StringSubstr(json, obj_start, i - obj_start + 1);
            ChartCtlDeployment d;
            d.id        = JsonStr("id", "", obj);
            d.expert    = JsonStr("expert", "", obj);
            d.templ     = JsonStr("template", "", obj);
            d.symbol    = JsonStr("symbol", "", obj);
            d.timeframe = JsonStr("timeframe", "", obj);
            // enabled is a bare JSON bool; desired.json emits it compactly.
            d.enabled = (StringFind(obj, "\"enabled\": false") < 0
                         && StringFind(obj, "\"enabled\":false") < 0);
            if(d.id != "")
            {
               int n = ArraySize(out);
               ArrayResize(out, n + 1);
               out[n] = d;
            }
            obj_start = -1;
         }
      }
      else if(ch == ']' && depth == 0)
         break;
   }
   return true;
}

string CChartControl::JsonEscape(const string s)
{
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\n", " ");
   StringReplace(out, "\r", " ");
   return out;
}

//+------------------------------------------------------------------+
ENUM_TIMEFRAMES CChartControl::TF(const string s)
{
   if(s == "M1")  return PERIOD_M1;
   if(s == "M2")  return PERIOD_M2;
   if(s == "M3")  return PERIOD_M3;
   if(s == "M4")  return PERIOD_M4;
   if(s == "M5")  return PERIOD_M5;
   if(s == "M6")  return PERIOD_M6;
   if(s == "M10") return PERIOD_M10;
   if(s == "M12") return PERIOD_M12;
   if(s == "M15") return PERIOD_M15;
   if(s == "M20") return PERIOD_M20;
   if(s == "M30") return PERIOD_M30;
   if(s == "H1")  return PERIOD_H1;
   if(s == "H2")  return PERIOD_H2;
   if(s == "H3")  return PERIOD_H3;
   if(s == "H4")  return PERIOD_H4;
   if(s == "H6")  return PERIOD_H6;
   if(s == "H8")  return PERIOD_H8;
   if(s == "H12") return PERIOD_H12;
   if(s == "D1")  return PERIOD_D1;
   if(s == "W1")  return PERIOD_W1;
   if(s == "MN1") return PERIOD_MN1;
   return PERIOD_H1;
}
//+------------------------------------------------------------------+
