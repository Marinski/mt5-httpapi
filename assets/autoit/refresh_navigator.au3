; refresh_navigator.au3  <window_match> <pid> <logpath>
; Refresh MT5's Navigator so the terminal sees an .ex5 that was copied into
; MQL5\Experts while it runs. Until then ChartApplyTemplate cannot load that
; expert (CHART_EXPERT_NAME stays empty), which breaks chart deployments of
; freshly uploaded experts. Does what a user would: right-click "Expert
; Advisors" in the Navigator and choose Refresh. NO #includes (an undefined
; include function pops a blocking error dialog).
;
; Writes RESULT=OK or RESULT=FAIL reason=<code> as the last log line.

Opt("WinTitleMatchMode", 2)
Opt("SendKeyDelay", 20)

Global Const $NAV_TREE = "SysTreeView321"
Global Const $EA_NODE_TEXT = "Expert Advisors"
Global Const $REFRESH_TEXT = "Refresh"
Global Const $REFRESH_CMD_ID = 33416   ; observed on build 6230; text is checked first
Global Const $WM_MN_GETHMENU = 0x01E1
Global Const $MF_BYPOSITION = 0x400

Global $gLog = -1

Func LogW($s)
   If $gLog <> -1 Then FileWrite($gLog, $s & @CRLF)
EndFunc

Func Finish($result, $code)
   LogW($result)
   If $gLog <> -1 Then FileClose($gLog)
   Exit $code
EndFunc

Func MenuItemText($hMenu, $pos)
   Local $buf = DllStructCreate("wchar[256]")
   DllCall("user32.dll", "int", "GetMenuStringW", "handle", $hMenu, "uint", $pos, _
           "ptr", DllStructGetPtr($buf), "int", 255, "uint", $MF_BYPOSITION)
   Return StringReplace(DllStructGetData($buf, 1), "&", "")
EndFunc

; ---- args: match, pid, logpath ----
If $CmdLine[0] < 3 Then Exit 10
Global $match = $CmdLine[1]
Global $pid = Int($CmdLine[2])
$gLog = FileOpen($CmdLine[3], 2)
If $gLog = -1 Then Exit 11
LogW("=== refresh_navigator match=" & $match & " pid=" & $pid & " ===")

; ---- find + activate this terminal's main window (visible, owned by pid) ----
Local $wl = WinList()
Local $hMT5 = 0
For $i = 1 To $wl[0][0]
   Local $h = $wl[$i][1]
   If $wl[$i][0] = "" Then ContinueLoop
   If BitAND(WinGetState($h), 2) = 0 Then ContinueLoop
   If $pid > 0 And WinGetProcess($h) <> $pid Then ContinueLoop
   If StringInStr($wl[$i][0], $match) > 0 Then $hMT5 = $h
Next
If $hMT5 = 0 Then Finish("RESULT=FAIL reason=mt5_window_not_found", 2)
WinActivate($hMT5)
Sleep(500)

; ---- select "Expert Advisors" under the Navigator's root node ----
Local $eaItem = ""
Local $children = ControlTreeView($hMT5, "", $NAV_TREE, "GetItemCount", "#0")
For $k = 0 To $children - 1
   If ControlTreeView($hMT5, "", $NAV_TREE, "GetText", "#0|#" & $k) = $EA_NODE_TEXT Then
      $eaItem = "#0|#" & $k
      ExitLoop
   EndIf
Next
If $eaItem = "" Then Finish("RESULT=FAIL reason=navigator_node_not_found", 3)
ControlFocus($hMT5, "", $NAV_TREE)
ControlTreeView($hMT5, "", $NAV_TREE, "Select", $eaItem)
Sleep(300)

; ---- open its context menu and find Refresh ----
Send("+{F10}")
Sleep(700)
Local $hMenuWnd = WinGetHandle("[CLASS:#32768]")
If @error Or $hMenuWnd = "" Then Finish("RESULT=FAIL reason=context_menu_not_open", 4)
Local $hMenu = DllCall("user32.dll", "handle", "SendMessageW", "hwnd", $hMenuWnd, _
                       "uint", $WM_MN_GETHMENU, "wparam", 0, "lparam", 0)[0]
Local $count = DllCall("user32.dll", "int", "GetMenuItemCount", "handle", $hMenu)[0]
Local $refreshPos = -1
For $i = 0 To $count - 1
   Local $id = DllCall("user32.dll", "uint", "GetMenuItemID", "handle", $hMenu, "int", $i)[0]
   If MenuItemText($hMenu, $i) = $REFRESH_TEXT Or $id = $REFRESH_CMD_ID Then $refreshPos = $i
Next
If $refreshPos < 0 Then
   Send("{ESC}")
   Finish("RESULT=FAIL reason=refresh_item_not_found", 5)
EndIf

; ---- choose it: Down from a fresh menu selects the first selectable item,
; and separators are skipped, so count the non-separator items up to it ----
Local $steps = 0
For $i = 0 To $refreshPos
   If MenuItemText($hMenu, $i) <> "" Then $steps += 1
Next
For $s = 1 To $steps
   Send("{DOWN}")
Next
Send("{ENTER}")
Sleep(1500)
Finish("RESULT=OK", 0)
