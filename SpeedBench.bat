@echo off
rem ============================================================
rem  Clash SpeedBench 启动器（Windows 双击运行）
rem  零第三方依赖，只需要系统里装有 Python 3.9+
rem ============================================================
chcp 65001 >nul
setlocal EnableExtensions

rem 数据目录：历史记录 / Web 令牌等放在 %APPDATA%\ClashSpeedBench，不污染源码目录
set "SPEEDBENCH_HOME=%APPDATA%\ClashSpeedBench"
if not exist "%SPEEDBENCH_HOME%" mkdir "%SPEEDBENCH_HOME%"

rem 切到本脚本所在目录（%~dp0 自带末尾反斜杠），保证面板能找到同目录的源码
cd /d "%~dp0"

rem 检测 Python；检测不到则引导到 Microsoft Store 安装（商店版自动配好 PATH）
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo 未检测到 Python，请先从 Microsoft Store 安装 Python 3
    echo （即将打开商店页面，安装完成后重新双击本文件即可）
    start ms-windows-store://pdp/?productid=9NRWMJP3717K
    pause
    exit /b 1
)

echo 已检测到 Python:
python --version
echo.

rem 故意用 python 而不是 pythonw：Windows 下面板的「中断测速」依赖
rem CTRL_BREAK_EVENT，而该事件只能发送给拥有控制台窗口的进程；
rem 用 pythonw（无控制台）会导致无法优雅中断测速、无法自动恢复 Clash 配置。
rem 因此这里保留一个最小化的控制台窗口，属于正常设计。
rem
rem 注意：speedbench_web.py 启动后会自行 webbrowser.open 打开浏览器，
rem 这里不再重复 start http://127.0.0.1:8950，否则会一次开出两个标签页。
start "Clash SpeedBench" /min python "%~dp0speedbench_web.py"

echo Clash SpeedBench 已启动，浏览器稍后会自动打开 http://127.0.0.1:8950
echo 面板运行在任务栏中最小化的 "Clash SpeedBench" 控制台窗口里：
echo 此窗口可最小化，关闭窗口将退出 SpeedBench
