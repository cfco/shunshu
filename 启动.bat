@echo off
setlocal enableextensions
cd /d "%~dp0"

rem 顺手启动脚本：双击后台起托盘（不留控制台窗口）。
rem 排错用：启动.bat debug   —— 用带控制台的方式启动，异常能直接看到。

set "PYW=.venv\Scripts\pythonw.exe"
set "PYEXE=.venv\Scripts\python.exe"

if /i "%~1"=="debug" goto :debug

if exist "%PYW%" goto :start
call :prepare
if errorlevel 1 exit /b 1

:start
start "" "%PYW%" -m shunshu run
echo [shunshu] 已在后台启动，托盘里找书本图标。
echo [shunshu] 退出：右键图标 - 退出。
ping -n 4 127.0.0.1 >nul
goto :eof

:debug
if not exist "%PYEXE%" call :prepare
if errorlevel 1 exit /b 1
echo [shunshu] 调试模式（关闭窗口即退出）
"%PYEXE%" -m shunshu run
goto :eof

:prepare
where uv >nul 2>nul
if errorlevel 1 (
    echo [shunshu] 找不到 .venv，也没有 uv。请先安装 uv，再执行：uv sync
    pause
    exit /b 1
)
echo [shunshu] 首次运行，正在安装依赖 ...
uv sync
if not exist "%PYW%" (
    echo [shunshu] uv sync 之后仍然没有 %PYW%
    pause
    exit /b 1
)
exit /b 0
