@echo off
REM ============================================================
REM adb-keepalive.bat - auto-reconnect loop for wireless adb
REM Run ON THE REMOTE DEV MACHINE (inside UU remote desktop).
REM Checks every 5s; reconnects when the device drops off.
REM
REM Usage: adb-keepalive.bat            (target 127.0.0.1:5555)
REM        adb-keepalive.bat ip:port    (any other target)
REM ============================================================
setlocal EnableDelayedExpansion
set ADB=C:\Users\Administrator\Downloads\jax-build\android-sdk\platform-tools\adb.exe

set TARGET=%~1
if "%TARGET%"=="" set TARGET=127.0.0.1:5555

echo keepalive target: %TARGET%
:loop
"%ADB%" devices | findstr /r /c:"%TARGET%.*device$" >nul
if errorlevel 1 (
  echo %date% %time% - device offline, reconnecting...
  "%ADB%" connect %TARGET%
)
timeout /t 5 /nobreak >nul
goto :loop
