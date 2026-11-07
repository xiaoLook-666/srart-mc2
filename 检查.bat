@echo off
title 启动器环境检查
cd /d "%~dp0"
echo ============================================
echo   MC 启动器 - 环境检查
echo ============================================
echo.
echo [1/4] Python 检测：
where py >nul 2>nul
if %errorlevel%==0 (echo        [OK] py 启动器已找到) else (echo        [缺失] py 启动器)
where python >nul 2>nul
if %errorlevel%==0 (echo        [OK] python 命令已找到) else (echo        [缺失] python 命令)
echo.
echo [2/4] tkinter 图形组件：
py -3 -c "import tkinter; print('        [OK] tkinter 可用')" 2>&1
echo.
echo [3/4] 启动器文件：
if exist launcher.py (echo        [OK] launcher.py 存在) else (echo        [缺失] launcher.py 缺失)
if exist mc_core.py (echo        [OK] mc_core.py 存在) else (echo        [缺失] mc_core.py 缺失)
echo.
echo [4/4] 游戏数据：
if exist ".minecraft\versions\1.12.1\1.12.1.jar" (echo        [OK] 1.12.1 已安装) else (echo        [未安装] 1.12.1)
if exist runtime (echo        [OK] Java 运行时目录存在) else (echo        [缺失] Java 运行时目录)
echo.
echo --------------------------------------------
echo 如果以上全部 [OK]，启动器应能正常打开。
echo 请把本窗口内容截图发给我排查。
echo --------------------------------------------
pause >nul
