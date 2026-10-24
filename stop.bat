@echo off
title Somnia·眠 - 停止
echo 正在停止 Somnia...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*somnia.panel*' }; if ($p) { $p | ForEach-Object { Write-Host ('  已停止 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host '  没有在运行' }"
ping -n 3 127.0.0.1 >nul
