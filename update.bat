@echo off
cd /d "%~dp0"
title Somnia·眠 - 更新
echo.
echo   把新版本的 zip 拖到本窗口上，然后回车；或者直接双击本文件走提示
echo   你的 data、memory、photos、stickers、persona.md 都不会被动
echo.
if "%~1"=="" (
    set /p ZIP=新版本 zip 的路径（直接拖进来也行）: 
) else (
    set ZIP=%~1
)
python "%~dp0scripts\update.py" "%ZIP%"
echo.
pause
