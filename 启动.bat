@echo off
setlocal enableextensions
cd /d "%~dp0"

rem shunshu launcher.
rem Default: start the tray icon in the background, no console window.
rem Debug:   run this file with the argument "debug" -> foreground, tracebacks stay visible.
rem Keep this file ASCII-only. cmd re-reads a .bat by byte offset, so a mid-file
rem "chcp 65001" combined with multi-byte text makes it resume at the wrong
rem position and execute comment fragments as commands. Chinese docs: README.md.

set "PYW=.venv\Scripts\pythonw.exe"
set "PYEXE=.venv\Scripts\python.exe"

if /i "%~1"=="debug" goto :debug

if exist "%PYW%" goto :start
call :prepare
if errorlevel 1 exit /b 1

:start
start "" "%PYW%" -m shunshu run
echo [shunshu] started in background, the tray icon should appear shortly.
echo [shunshu] to quit: right-click the tray icon, then exit.
ping -n 4 127.0.0.1 >nul
goto :eof

:debug
if not exist "%PYEXE%" call :prepare
if errorlevel 1 exit /b 1
echo [shunshu] debug mode, running in foreground; close this window to quit.
"%PYEXE%" -m shunshu run
goto :eof

:prepare
where uv >nul 2>nul
if errorlevel 1 (
    echo [shunshu] no .venv and no uv found. Install uv first, then run: uv sync
    pause
    exit /b 1
)
echo [shunshu] preparing the virtual environment, running uv sync ...
uv sync
if not exist "%PYW%" (
    echo [shunshu] still missing %PYW% after uv sync
    pause
    exit /b 1
)
exit /b 0
