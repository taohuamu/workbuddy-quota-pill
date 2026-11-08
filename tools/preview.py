"""构造小窗 → 等数据 → 用 PrintWindow 抓自身窗口 → 存 BMP → 退出。

必用这个而不是屏幕截图：分层窗口（-transparentcolor）用 BitBlt 抓出来是全黑的。

用法:
    python tools/preview.py out.bmp [--dark] [--pill] [--report] [--hover] [--full]
                                    [--home <伪造的 WORKBUDDY_HOME>] [--max-rows N]

--full 关掉「展开时不压输入框」，卡片按设计宽度展开 —— 截图用，避免宽度随
当时输入框的位置变化。
"""
import ctypes
import os
import struct
import sys
import time
import traceback
from ctypes import wintypes

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

out = sys.argv[1]
rest = sys.argv[2:]
dark = "--dark" in rest
if dark:
    rest.remove("--dark")
if "--hover" in rest:
    rest.remove("--hover")
PILL = "--pill" in rest
if PILL:
    rest.remove("--pill")
REPORT = "--report" in rest
if REPORT:
    rest.remove("--report")
FULL = "--full" in rest
if FULL:
    rest.remove("--full")
if "--home" in rest:
    i = rest.index("--home")
    os.environ["WORKBUDDY_HOME"] = rest[i + 1]
    del rest[i:i + 2]

import quota_tray as qt  # noqa: E402

if dark:
    qt.system_is_dark = lambda: True

qt.enable_dpi_awareness()
args = qt.build_parser().parse_args(["--always"] + rest)
w = qt.TrayWindow(args)
if FULL:
    # 截图用：关掉「展开时不压输入框」，卡片按设计宽度 CARD_W 展开。
    # 否则宽度取决于当时输入框右缘的位置，同一份代码在不同桌面下截出来宽窄不一。
    w._avoid_input = False
    w._plan_cache = None
w.root.deiconify()
w.root.update()

deadline = time.time() + 6
while time.time() < deadline:
    w.root.update()
    if not w._busy and (w.report.get("models") or w.report.get("error")):
        break
    time.sleep(0.05)
time.sleep(0.35)
w.root.update()
if "--hover" in sys.argv:
    w._hover_row = 0
    w.root.update()
if PILL:
    w._set_collapsed(True)
    w.root.update()
else:
    w._set_collapsed(False)
    w.root.update()
if REPORT:
    w.open_report()
    for _ in range(30):
        w.root.update()
        time.sleep(0.05)
    # 截图不想要「保存为默认位置」那枚 chip —— 它只在拖过之后出现，属于交互状态
    win = getattr(w, "_report_win", None)
    if win is not None:
        win._pos_dirty = False
        win._render_head()
        w.root.update()

try:
    w.render()
    w.root.update()
except Exception:
    traceback.print_exc()

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


TITLE = "配额胶囊 · 完整报告" if REPORT else "配额胶囊"
hits = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
def cb(h, _):
    n = u32.GetWindowTextLengthW(h)
    if n > 0:
        b = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, b, n + 1)
        if b.value == TITLE:
            hits.append(h)
    return True


u32.EnumWindows(cb, 0)
if not hits:
    print("找不到窗口")
    sys.exit(1)
h = hits[0]
r = RECT()
u32.GetWindowRect(h, ctypes.byref(r))
ww, hh = r.right - r.left, r.bottom - r.top

srcdc = u32.GetDC(0)
memdc = g32.CreateCompatibleDC(srcdc)
bmp = g32.CreateCompatibleBitmap(srcdc, ww, hh)
g32.SelectObject(memdc, bmp)
u32.PrintWindow(h, memdc, 2)

bi = BIH()
bi.biSize = ctypes.sizeof(bi)
bi.biWidth = ww
bi.biHeight = -hh
bi.biPlanes = 1
bi.biBitCount = 32
buf = ctypes.create_string_buffer(ww * hh * 4)
g32.GetDIBits(memdc, bmp, 0, hh, buf, ctypes.byref(bi), 0)
with open(out, "wb") as fh:
    fh.write(b"BM")
    fh.write(struct.pack("<IHHI", 54 + len(buf.raw), 0, 0, 54))
    fh.write(bytes(bi))
    fh.write(buf.raw)

print(f"ok {ww}x{hh} dark={w._dark} -> {out}")
print("rows:", [(x["model"], x["percent_used"], x["level"]) for x in w.report.get("models", [])])
w.root.destroy()
