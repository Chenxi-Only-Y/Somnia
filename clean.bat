@echo off
cd /d "%~dp0"
title Somnia·眠 - 清理
echo.
echo   打包前清理：会删掉本机的对话、密钥、日志、照片
echo   表情包 stickers\ 属于项目内容，不动它
echo   人设 persona.md 会被删（只留模板）
echo.
choice /c YN /m "确认清理"
if errorlevel 2 goto :eof

for %%d in (data memory wechatauto_logs photos) do (
    if exist "%%d" (
        rmdir /s /q "%%d"
        echo   删除 %%d
    )
)
if exist "persona\persona.md" (
    del /q "persona\persona.md"
    echo   删除 persona\persona.md
)
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
del /q *.pyc 2>nul

echo.
echo   完成。stickers\ 和 persona\SOUL.md 会留下，它们要跟着包走。
echo.
dir /b
pause
