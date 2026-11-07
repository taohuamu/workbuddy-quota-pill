"""屏幕区域截图：python tools/shot.py out.bmp x y w h"""
import ctypes, os, struct, sys
from ctypes import wintypes

try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32 = u32
gdi32 = g32

out = sys.argv[1]
x = int(sys.argv[2]); y = int(sys.argv[3]); w = int(sys.argv[4]); h = int(sys.argv[5])

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]

srcdc = u32.GetDC(0)
memdc = g32.CreateCompatibleDC(srcdc)
bmp = g32.CreateCompatibleBitmap(srcdc, w, h)
g32.SelectObject(memdc, bmp)
if not g32.BitBlt(memdc, 0, 0, w, h, srcdc, x, y, 0x40CC0020):
    raise SystemExit("BitBlt failed")

bi = BITMAPINFOHEADER()
bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
bi.biWidth = w
bi.biHeight = -h
bi.biPlanes = 1
bi.biBitCount = 32
bi.biCompression = 0
stride = w * 4
buf = ctypes.create_string_buffer(stride * h)
if not g32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
    raise SystemExit("GetDIBits failed")

with open(out, "wb") as fh:
    fh.write(b"BM")
    fh.write(struct.pack("<IHHI", 14 + 40 + len(buf), 0, 0, 54))
    fh.write(bytes(bi))
    fh.write(buf.raw)
print(out, w, h)
