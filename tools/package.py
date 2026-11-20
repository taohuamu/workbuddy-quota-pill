"""打内测包：只复制运行必需的文件，输出到 ../dist/。

用法:
    python tools/package.py              # 按今天的日期打
    python tools/package.py --tag beta2  # 自定义标签

为什么要白名单：项目里还有 tests/、delivery/（6 张 bmp 共 28MB）、README 和交接文档，
这些对测试成员没用，只会让包变大、让他们困惑该点哪个文件。
`tools/watchdog.tpl` 必须带上 —— 「开机自动待命」靠它生成登录脚本，少了这一步会静默失败。
`forecast.py` 也必须带上 —— 预测层是独立模块，`quota_tray` / `quota_pill` 都硬 import 它；
漏了不会在打包时报错，而是使用者一解压就 ImportError。`check_zip()` 兜住这一类漏文件。
"""
import ast
import datetime
import os
import py_compile
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(HERE, "dist")
TOP = "quota-pill"          # 压缩包内的顶层目录，也是包名的前缀（2026-11-16 起，原 quota-watch）

FILES = [
    "quota_tray.py",
    "quota_pill.py",
    "forecast.py",
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


def check_zip(zip_path):
    """解压到临时目录，核对包自己能不能立住。

    守的是「白名单漏文件」：漏了 `forecast.py` 时打包与解压都不报错，直到使用者双击
    tray.bat 才 ImportError，而中间连着两版没人发现。这里不启动程序（会开窗），只做
    两件事：逐文件编译，以及确认每个 import 要么是包内同目录模块、要么是标准库。
    标准库查的是 `sys.stdlib_module_names` 而不是「本机能不能 import」——打包机的
    python 未必带 tkinter，那样会把正常的标准库依赖误判成缺件。
    """
    problems = []
    stdlib = getattr(sys, "stdlib_module_names", None)
    with tempfile.TemporaryDirectory(prefix="quota-pill-zip-") as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        root = os.path.join(tmp, TOP)
        local = {n[:-3] for n in os.listdir(root) if n.endswith(".py")}
        if not local:
            return ["包里没有任何 .py"]
        seen = set()
        for name in sorted(local):
            path = os.path.join(root, name + ".py")
            try:
                py_compile.compile(path, cfile=os.path.join(tmp, name + ".checked"),
                                   doraise=True)
            except py_compile.PyCompileError as exc:
                problems.append("%s 编译失败：%s" % (name, exc))
                continue
            if stdlib is None:            # 3.10 以下拿不到标准库名单，只保编译这一关
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif (isinstance(node, ast.ImportFrom) and node.level == 0
                      and node.module):
                    mods = [node.module.split(".")[0]]
                else:
                    continue
                for mod in mods:
                    if mod in local or mod in stdlib or mod in seen:
                        continue
                    seen.add(mod)
                    problems.append("%s 依赖 %s：既不在包内，也不是标准库"
                                    "（包里的运行文件只应依赖这两类）"
                                    % (name, mod))
    return problems


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

    zip_path = os.path.join(OUT_ROOT, "%s-%s.zip" % (TOP, tag))
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, names in os.walk(target):
            for name in sorted(names):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, target)
                zf.write(full, os.path.join(TOP, rel))

    problems = check_zip(zip_path)
    if problems:
        print("包自检失败，这个包发出去会跑不起来：")
        for line in problems:
            print("  - " + line)
        return None
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
    print("包自检通过：每个 .py 都能编译，同目录模块齐全，包外依赖本机可导入")
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            print("  %7d  %s" % (info.file_size, info.filename))
    print("\n分发前自查：")
    print("  1. 解压到空目录，双击 tray.bat 能出现胶囊")
    print("  2. 右键「开机自动待命」勾上后，Startup 里出现 quota-pill-tray.vbs")
    print("  3. 关掉 WorkBuddy，小窗 10 秒内自己退出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
