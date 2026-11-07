@echo off
chcp 936 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo 未找到 python，请先安装 Python 3.8+ 并保留 tcl/tk 组件。
    pause
    exit /b 2
)
python -c "import sys; from quota_tray import spawn_detached; sys.exit(spawn_detached())"
if errorlevel 2 (
    echo.
    echo 启动失败：多半是当前 Python 没带 tkinter。
    echo 运行 python quota_tray.py --which 可查看本机哪些解释器可用。
    pause
    exit /b 2
)
if errorlevel 1 (
    echo.
    echo 启动失败，请检查上面的报错。
    pause
    exit /b 1
)
exit /b 0
