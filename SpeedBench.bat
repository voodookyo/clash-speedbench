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

rem Deliberately "python", not "pythonw": the panel's cancel-benchmark
rem button relies on CTRL_BREAK_EVENT, which can only be delivered to a
rem process that owns a console window. pythonw (no console) would break
rem graceful cancel and the automatic restore of the Clash config, so the
rem minimized console window that stays open is by design.
rem
rem speedbench_web.py opens the browser by itself once it is up; do NOT
rem "start http://127.0.0.1:8950" here or two tabs would open at once.
start "Clash SpeedBench" /min python "%~dp0speedbench_web.py"

rem System tray icon: launched through the .vbs wrapper so no extra console
rem window flashes. The tray polls the panel and exits automatically with it.
start "" wscript //nologo "%~dp0SpeedBenchTray.vbs"

echo Clash SpeedBench started; the browser opens http://127.0.0.1:8950 shortly.
echo The panel runs in the minimized "Clash SpeedBench" console window on
echo the taskbar, plus a tray icon near the clock (left-click opens the panel).
echo Closing that window exits SpeedBench.
