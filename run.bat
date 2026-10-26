@echo off
rem ============================================================
rem  抖音福袋挂机 启动脚本
rem  用法：直接双击，或在命令行执行 run.bat
rem  注意：本文件按中文 Windows 控制台编码(GBK/cp936)保存，
rem        若系统默认代码页不是 936，脚本里的中文可能显示为乱码，
rem        但功能不受影响。
rem ============================================================
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 抖音福袋挂机

set "PY=.venv\Scripts\python.exe"

if /i "%~1"=="check" goto :mode_check
if /i "%~1"=="probe" goto :mode_probe
if /i "%~1"=="help"  goto :usage
if /i "%~1"=="?"     goto :usage

echo ==========================================
echo    抖音福袋挂机
echo ==========================================
echo.

rem ---------- 1) 虚拟环境 ----------
if not exist "%PY%" (
    echo [错误] 没找到虚拟环境：%PY%
    echo.
    echo 请先在本目录执行下面两条命令：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo [1/3] Python 环境 .......... OK

rem ---------- 2) adb ----------
where adb >nul 2>nul
if errorlevel 1 (
    echo [2/3] adb ................. 未找到！
    echo.
    echo 请安装 Android Platform Tools，并把 adb 所在目录加入 PATH
    echo.
    pause
    exit /b 1
)
echo [2/3] adb ................. OK

rem ---------- 3) 手机 ----------
rem adb devices 每行形如：<序列号>  device / unauthorized / offline
set "DEVICE="
set "STATE="
for /f "tokens=1,2" %%a in ('adb devices 2^>nul') do (
    if "%%b"=="device" set "DEVICE=%%a"
    if "%%b"=="unauthorized" set "STATE=unauthorized"
    if "%%b"=="offline" set "STATE=offline"
)
if defined DEVICE goto :dev_ok
if "%STATE%"=="unauthorized" goto :dev_unauth
if "%STATE%"=="offline" goto :dev_offline
goto :dev_none

:dev_ok
echo [3/3] 手机 ................ OK  [%DEVICE%]
goto :launch

:dev_unauth
echo [3/3] 手机 ................ 未授权！
echo.
echo   手机屏幕上应该有个"允许USB调试吗"的弹窗，点【允许】。
echo   没看到弹窗的话：进开发者选项把 USB 调试关掉再打开，然后重新插线。
echo.
pause
goto :launch

:dev_offline
echo [3/3] 手机 ................ offline（连上了，但 adb 通信异常）
echo.
echo   依次试试：
echo     1. 执行  adb kill-server  然后重新运行本脚本
echo     2. 换一根数据线，或换一个 USB 口
echo     3. 手机重新确认一次 USB 调试授权，并确认屏幕没锁
echo.
pause
goto :launch

:dev_none
echo [3/3] 手机 ................ 未检测到设备！
echo.
echo   请依次检查：
echo     1. 数据线插好，手机弹出的"允许USB调试"要点允许
echo     2. 小米/红米还要额外打开「USB调试（安全设置）」，
echo        否则点击会报 SecurityException，表现为画面一直不动
echo     3. 抖音先进到一个直播间：我 - 关注 - 直播中 - 任意直播间
echo     4. 别让屏幕锁上：开发者选项 - 充电时不锁定屏幕
echo.
echo   程序每 10 秒会重试一次，插上手机后会自动开始。
echo.
pause
goto :launch

:launch
echo.
echo 正在启动（点悬浮窗右上角 X 或右键即退出）...
echo.
"%PY%" douyin_guaji.py %*
echo.
echo 程序已退出。
pause
exit /b 0

rem ============================================================
:mode_check
echo 运行环境自检（不需要连接手机），大约需要 30 秒...
echo.
if not exist "%PY%" (
    echo [错误] 没找到虚拟环境：%PY%
    pause
    exit /b 1
)
"%PY%" tests\smoke_test.py
echo.
pause
exit /b 0

rem ============================================================
:mode_probe
echo 真机探针：分辨率自检 + 房名区域 OCR
echo.
echo 请先把手机进入一个直播间，然后按任意键开始...
pause >nul
if not exist "%PY%" (
    echo [错误] 没找到虚拟环境：%PY%
    pause
    exit /b 1
)
"%PY%" tools\probe_device.py
echo.
pause
exit /b 0

rem ============================================================
:usage
echo 用法：
echo     run.bat              启动挂机（带悬浮窗）
echo     run.bat --no-gui     启动挂机（不要悬浮窗）
echo     run.bat check        环境自检（不需要手机）
echo     run.bat probe        真机探针（需要手机在直播间）
echo     run.bat help         显示本说明
echo.
pause
exit /b 0
