@echo off
REM ============================================================
REM adb-setup-phone.bat - one-time setup for wireless adb
REM Switches phone adbd to FIXED tcpip 5555 mode so that the
REM UU remote port-mapping rule never needs to change again.
REM
REM Usage:
REM   adb-setup-phone.bat <phone-ip:wireless-debug-port>
REM   Example: adb-setup-phone.bat 192.168.1.50:37847
REM
REM The port is the one shown in Developer options ->
REM Wireless debugging -> "IP address & port" (RANDOM, changes
REM on toggle/WiFi reconnect - that is why we lock to 5555).
REM If this machine was never paired with the phone, first run:
REM   adb pair <phone-ip>:<pairing-port> <6-digit-code>
REM ============================================================
setlocal
set ADB=C:\Users\Administrator\Downloads\jax-build\android-sdk\platform-tools\adb.exe

if "%~1"=="" (
  echo Usage: adb-setup-phone.bat ^<phone-ip:wireless-debug-port^>
  echo Example: adb-setup-phone.bat 192.168.1.50:37847
  exit /b 1
)

echo [1/4] adb connect %~1 ...
"%ADB%" connect %~1
if errorlevel 1 goto :fail

echo [2/4] switch adbd to fixed tcpip 5555 ...
"%ADB%" -s %~1 tcpip 5555
if errorlevel 1 goto :fail

timeout /t 3 /nobreak >nul

for /f "tokens=1 delims=:" %%a in ("%~1") do set PHONE_IP=%%a

echo [3/4] reconnect on fixed port %PHONE_IP%:5555 ...
"%ADB%" connect %PHONE_IP%:5555

echo [4/4] device list:
"%ADB%" devices
echo.
echo DONE. Now create ONE permanent UU port-mapping rule:
echo   remote 127.0.0.1:5555  --^>  local %PHONE_IP%:5555
echo After that, this machine only ever needs: adb connect 127.0.0.1:5555
exit /b 0

:fail
echo FAILED. Likely causes: wrong port (check Wireless debugging screen),
echo phone not paired with this machine (run: adb pair ip:pairport code),
echo or phone battery optimization killed adbd.
exit /b 1
