' SpeedBench tray hidden launcher.
' Runs the PowerShell tray script with no console window (wscript + Run 0).
' NOTE: keep this file pure ASCII - Windows script hosts read .vbs as ANSI,
' CJK comments would mojibake (harmless in comments, but keep it clean anyway).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
ps1 = fso.GetParentFolderName(WScript.ScriptFullName) & "\SpeedBenchTray.ps1"
sh.Run "powershell -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, False
