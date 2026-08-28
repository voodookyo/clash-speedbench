@echo off
chcp 65001 >nul
rem ============================================================
rem  Clash SpeedBench launcher (double-click to run on Windows)
rem  Zero third-party dependencies: needs only Python 3.9+
rem ============================================================
rem  WARNING: this file must stay 100% ASCII, no BOM. cmd.exe parses
rem  batch files through the console codepage, and a UTF-8 .bat with
rem  CJK text is mis-parsed on a zh-CN Windows under both CP936 and
rem  CP65001: comment/echo fragments end up executed as commands
rem  ("is not recognized as an internal or external command").
rem  Reproduced on real hardware during the v0.8.0 Windows acceptance.
setlocal EnableExtensions

rem Data dir: history DB / web token live in %APPDATA%\ClashSpeedBench,
rem keeping the source directory clean.
set "SPEEDBENCH_HOME=%APPDATA%\ClashSpeedBench"
if not exist "%SPEEDBENCH_HOME%" mkdir "%SPEEDBENCH_HOME%"

rem Switch to this script's directory (%~dp0 ends with a backslash) so
rem the panel can find the source files sitting next to it.
cd /d "%~dp0"

rem Detect Python; if missing, open the Microsoft Store install page
rem (the Store build sets up PATH by itself), then exit.
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python not found. Please install Python 3 from the Microsoft Store
    echo ^(the store page opens now; double-click this file again afterwards^).
    start ms-windows-store://pdp/?productid=9NRWMJP3717K
    pause
    exit /b 1
)

echo Python detected:
python --version
echo.

rem Prefer pythonw (no console window): the panel no longer needs a console
rem for the cancel button - cancellation now travels through a sentinel file
rem (SPEEDBENCH_CANCEL_FILE), and benchmark/grandchild processes are spawned
rem with CREATE_NO_WINDOW. If pythonw is unavailable (rare; the Microsoft
rem Store build ships it), fall back to python with a minimized console.
rem Panel output goes to web.log either way so crashes stay diagnosable.
rem
rem speedbench_web.py opens the browser by itself once it is up; do NOT
rem "start http://127.0.0.1:8950" here or two tabs would open at once.
where pythonw >nul 2>nul
if errorlevel 1 (
    echo pythonw not found; falling back to a minimized console window.
    start "Clash SpeedBench" /min python "%~dp0speedbench_web.py" >>"%SPEEDBENCH_HOME%\web.log" 2>&1
) else (
    start "" pythonw "%~dp0speedbench_web.py" >>"%SPEEDBENCH_HOME%\web.log" 2>&1
)

echo Clash SpeedBench started; the browser opens http://127.0.0.1:8950 shortly.
echo No console window stays open; use the tray icon near the clock
echo ^(left-click opens the panel, right-click quits SpeedBench^).
