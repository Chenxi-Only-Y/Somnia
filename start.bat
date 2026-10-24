@echo off
cd /d "%~dp0"
title Somnia·眠
echo ==========================================
echo   Somnia 控制台
echo   http://127.0.0.1:8765
echo   关掉这个窗口 = 停止
echo ==========================================
echo.
powershell -NoProfile -Command "$c = (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*somnia.panel*' } | Measure-Object).Count; if ($c -gt 0) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo 已经在运行了，直接打开面板。
  start "" http://127.0.0.1:8765
  ping -n 3 127.0.0.1 >nul
  exit /b
)
start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start "" http://127.0.0.1:8765"
python -u -m somnia.panel --port 8765
echo.
echo ------------------------------------------
echo 进程已停止。
echo 如果是意外退出，看上面最后几行报错。
echo ------------------------------------------
pause
