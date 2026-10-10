@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0"

rem 顺手启动脚本：默认后台启动托盘常驻图标；debug 参数则前台运行方便排查异常

set "PYW=.venv\Scripts\pythonw.exe"
set "PYEXE=.venv\Scripts\python.exe"

if /i "%~1"=="debug" goto :debug

if exist "%PYW%" goto :start
call :prepare
if errorlevel 1 exit /b 1

:start
start "" "%PYW%" -m shunshu run
echo [shunshu] 后台启动完成，托盘图标即将出现。
echo [shunshu] 右键托盘图标 - 退出
ping -n 4 127.0.0.1 >nul
goto :eof

:debug
if not exist "%PYEXE%" call :prepare
if errorlevel 1 exit /b 1
echo [shunshu] 调试模式，前台运行
"%PYEXE%" -m shunshu run
goto :eof

:prepare
where uv >nul 2>nul
if errorlevel 1 (
    echo [shunshu] 没有 .venv 也没有 uv，请先安装 uv 后执行：uv sync
    pause
    exit /b 1
)
echo [shunshu] 正在准备虚拟环境并安装依赖 ...
uv sync
if not exist "%PYW%" (
    echo [shunshu] uv sync 之后仍然没有 %PYW%
    pause
    exit /b 1
)
exit /b 0
