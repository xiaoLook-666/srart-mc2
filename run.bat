@echo off
title MC 启动器
cd /d "%~dp0"

rem 依次尝试可用的 Python
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

if not defined PY (
    echo [错误] 未找到 Python。请到 python.org 安装 Python 3 并勾选 Add to PATH。
    pause
    exit /b 1
)

%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前 Python 缺少 tkinter 组件。
    pause
    exit /b 1
)

%PY% launcher.py
if errorlevel 1 (
    echo.
    echo [启动器异常退出] 错误详情已写入 launcher_error.log，请把该文件发给我排查。
    pause
)
