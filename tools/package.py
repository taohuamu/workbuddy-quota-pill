"""打内测包：只复制运行必需的文件，输出到 ../dist/。

用法:
    python tools/package.py              # 按今天的日期打
    python tools/package.py --tag beta2  # 自定义标签

为什么要白名单：项目里还有 tests/、delivery/（6 张 bmp 共 28MB）、README 和交接文档，
这些对测试成员没用，只会让包变大、让他们困惑该点哪个文件。
`tools/watchdog.tpl` 必须带上 —— 「开机自动待命」靠它生成登录脚本，少了这一步会静默失败。
"""
import datetime
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(HERE, "dist")
TOP = "quota-watch"

FILES = [
    "quota_tray.py",
    "quota_watch.py",
    "tray.bat",
    "run.bat",
    "使用说明.md",
    os.path.join("tools", "watchdog.tpl"),
]

UNINSTALL_BAT = """@echo off
chcp 936 >nul
cd /d "%~dp0"
echo 正在移除开机自启...
where python >nul 2>nul
if errorlevel 1 (
    echo 找不到 python，跳过这一步。
    echo 请在右键菜单里手动取消勾选「开机自动待命」。
    pause
    exit /b 1
)
python -c "import quota_tray as q; ok, msg = q.uninstall_startup(); print(msg)"
echo.
echo 完成。接下来请右键小窗选择「退出」，再删除本文件夹。
pause
"""


def build(tag):
    target = os.path.join(OUT_ROOT, TOP)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(os.path.join(target, "tools"), exist_ok=True)

    missing = []
    for rel in FILES:
        src = os.path.join(HERE, rel)
        if not os.path.exists(src):
            missing.append(rel)
            continue
        dst = os.path.join(target, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    if missing:
        print("缺少文件：" + "、".join(missing))
        return None

    # 卸载脚本要 GBK：cmd 默认按系统代码页读 .bat，UTF-8 的中文会变乱码
    with open(os.path.join(target, "卸载.bat"), "w", encoding="gbk",
              newline="\r\n") as fh:
        fh.write(UNINSTALL_BAT)

    zip_path = os.path.join(OUT_ROOT, "quota-watch-%s.zip" % tag)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, names in os.walk(target):
            for name in sorted(names):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, target)
                zf.write(full, os.path.join(TOP, rel))
    return zip_path


def main(argv):
    tag = "beta-" + datetime.date.today().strftime("%Y%m%d")
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]
    os.makedirs(OUT_ROOT, exist_ok=True)
    path = build(tag)
    if not path:
        return 2
    size = os.path.getsize(path) / 1024.0
    print("打成：%s（%.0f KB）" % (path, size))
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            print("  %7d  %s" % (info.file_size, info.filename))
    print("\n分发前自查：")
    print("  1. 解压到空目录，双击 tray.bat 能出现胶囊")
    print("  2. 右键「开机自动待命」勾上后，Startup 里出现 quota-watch-tray.vbs")
    print("  3. 关掉 WorkBuddy，小窗 10 秒内自己退出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
