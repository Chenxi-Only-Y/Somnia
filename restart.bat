@echo off
cd /d "%~dp0"
title Somnia·眠 - 重启
echo 停止旧的...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*somnia.panel*' }; if ($p) { $p | ForEach-Object { Write-Host ('  停止 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host '  没有在运行' }"
ping -n 3 127.0.0.1 >nul
echo 启动新的...
call "%~dp0start.bat"
