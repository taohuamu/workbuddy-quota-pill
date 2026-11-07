@echo off
chcp 936 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo 未找到 python，请先安装 Python 3.8+ 并加入 PATH。
    pause
    exit /b 2
)
python quota_watch.py %*
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo [提示] 退出码 %CODE%：存在触限风险。
pause
exit /b %CODE%
