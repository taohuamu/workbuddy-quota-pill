#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 配额胶囊 · 右下角常驻小窗
==================================
展示层：所有数字都来自 quota_watch.build_report()，本文件不重复任何判定逻辑，
也不碰配额快照的解析与取整口径。

生命周期（跟随 WorkBuddy 主窗口，而非仅进程）：
    打开 WorkBuddy      → 小窗出现
    关闭主窗口 / 退到托盘 → 小窗隐藏
    真退出 WorkBuddy    → 小窗进程退出（--exit-on-close 可改为关闭即退出）

之所以要同时看「进程」和「可见窗口」：WorkBuddy 支持关闭到托盘，
只看进程的话关窗后小窗会赖着不走；只看窗口的话启动瞬间会闪一下。

用法：
    python quota_tray.py              # 常驻小窗
    python quota_tray.py --print      # 不开窗，只输出摘要行（可塞状态栏）
    python quota_tray.py --always     # 不跟随 WorkBuddy，直接显示（调试/预览）
    python quota_tray.py --install    # 写入登录启动项，开机后静默待命
    python quota_tray.py --uninstall  # 移除启动项

图形界面依赖标准库 tkinter（Windows 官方安装包自带）。
若当前解释器缺 tkinter，用 `python quota_tray.py --which` 查看可用解释器。
"""
import ctypes
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quota_watch as qw  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_MARK = "workbuddy"                      # 进程名前缀，WorkBuddy / WorkBuddyX 都算
STARTUP_NAME = "quota-watch-tray.vbs"
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "quota-watch")
STATE_FILE = os.path.join(STATE_DIR, "tray.json")
LOG_FILE = os.path.join(STATE_DIR, "tray.log")
SUPPRESS_FILE = os.path.join(STATE_DIR, "suppress")   # 手动关窗后 15 分钟内别自动弹回

# ── 配色：两套调色板，跟随系统深浅色 ─────────────────────────────────────
# 透明色键：Tk 的无边框窗口靠它抠出圆角，任何设计里都不能用到这个颜色。
C_KEY = "#ff00ff"

PALETTE = {
    "light": {
        "card": "#ffffff",
        "edge": "#e3e7ef",
        "edge_soft": "#eef1f7",
        "text": "#15181d",
        "sub": "#767e8c",
        "track": "#e5eaf2",
        "hover": "#f6f8fc",
        "shadow": ("#f3f6fa", "#edf0f6", "#e7ecf3"),
        "close_hover_bg": "#fdecea",
        "close_hover": "#d93025",
        "toast": "#16803c",
        # 完整报告窗的 1px 轮廓：比 edge 再浅半档，只有轮廓感、没有「框」的感觉
        "win_edge": "#e8ebf0",
        # 报告窗标题栏与卡片同色（浅色=白），整窗融进宿主应用的白色系
        "head_bg": "#ffffff",
        "head_fg": "#15181d",
        "level": {
            "critical": "#d93025",
            "warn": "#d97a08",
            "ok": "#16803c",
            "idle": "#9aa1ab",
        },
    },
    "dark": {
        "card": "#202227",
        "edge": "#3a3f47",
        "edge_soft": "#2b2f36",
        "text": "#e9ecf1",
        "sub": "#8e96a3",
        "track": "#343941",
        "hover": "#22262c",
        "shadow": ("#0f1013", "#121316", "#15171a"),
        "close_hover_bg": "#3a2320",
        "close_hover": "#ff6b5e",
        "toast": "#3dd68c",
        "win_edge": "#3a3f47",
        # 深色主题：标题栏与卡片同色，同样只靠分隔线分区
        "head_bg": "#202227",
        "head_fg": "#e9ecf1",
        "level": {
            "critical": "#ff6b5e",
            "warn": "#f5a524",
            "ok": "#3dd68c",
            "idle": "#7c838d",
        },
    },
}

# 兼容旧引用（命令行/无主题场景默认取浅色）
C_CARD = PALETTE["light"]["card"]
C_EDGE = PALETTE["light"]["edge"]
C_TEXT = PALETTE["light"]["text"]
C_SUB = PALETTE["light"]["sub"]
C_TRACK = PALETTE["light"]["track"]
LEVEL_ACCENT = PALETTE["light"]["level"]

LEVEL_CN = {"critical": "危险", "warn": "注意", "ok": "正常", "idle": "待观察"}
LEVEL_ORDER = {"critical": 0, "warn": 1, "ok": 2, "idle": 3}


def log(msg):
    """pythonw 下 sys.stdout/stderr 都是 None，任何 print 都会炸。

    有控制台就顺手打印，同时始终落盘一份（超过 256KB 轮转成 .old），
    事后排查靠它 —— 没有日志的无痕死亡没法查。
    """
    line = "%s %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    try:
        if sys.stdout:
            print(line)
    except Exception:
        pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 262144:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 纯函数区（无 GUI、无系统调用，测试直接覆盖）
# ══════════════════════════════════════════════════════════════════════

def text_units(s):
    """按视觉宽度估算字符数：CJK/全角记 2，ASCII 记 1。"""
    n = 0
    for ch in s:
        n += 2 if ord(ch) > 0x2E7F else 1
    return n


def elide_units(s, max_units):
    """按视觉宽度截断，超出部分用省略号；max_units 过小时只留省略号。"""
    if max_units <= 0:
        return ""
    if text_units(s) <= max_units:
        return s
    if max_units <= 2:
        return "…"
    out, used = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0x2E7F else 1
        if used + w > max_units - 2:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def worst_level(rows):
    """多模型里最严重的档位，用于小窗指示灯与边框。"""
    if not rows:
        return "idle"
    return min((r.get("level", "idle") for r in rows), key=lambda l: LEVEL_ORDER.get(l, 9))


MODEL_SUFFIXES = ("-flash", "-pro", "-mini", "-turbo", "-preview", "-latest", "-thinking")


def short_model(name):
    """胶囊里的模型名：太长就先丢能力后缀，比硬截断成 "xxx-fl…" 好看。"""
    name = str(name or "")
    if len(name) < 16:
        return name
    for suf in MODEL_SUFFIXES:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def live_models(rep):
    """本周期还有效的模型。

    `stale` 由数据层标出：最后一条快照的重置时刻已经过去 = 本周期还没用过它，
    它的百分比是上一周期的残值，显示出来会让人以为「11% 还占着额度」。
    """
    return [r for r in (rep.get("models") or []) if not r.get("stale")]


def latest_row(rep):
    """最近一次对话用到的模型。

    胶囊上要显示的是「刚刚用的那个模型」，不是最危险的那个 —— 后者留给展开卡片排序。
    优先按 `last_seen_ts`（数值）比较：`last_seen` 是 "MM-DD HH:MM" 字符串，
    跨年时 12-31 会排在 01-01 前面，去年用过的模型会被误判成最近的。
    """
    rows = live_models(rep)
    if not rows:
        return None
    timed = [r for r in rows if isinstance(r.get("last_seen_ts"), (int, float))]
    if timed:
        return max(timed, key=lambda r: (float(r["last_seen_ts"]),
                                         int(r.get("requests") or 0)))
    # 老数据没有数值时间戳时退回字符串比较，行为与之前一致
    return max(rows, key=lambda r: (str(r.get("last_seen") or ""),
                                    int(r.get("requests") or 0)))


def pick_rows(rep, limit=3):
    """报告 → (展示用前 N 行, 模型总数)。排序与 build_report 一致：先危险后百分比。"""
    models = live_models(rep)
    ordered = sorted(models, key=lambda r: (LEVEL_ORDER.get(r.get("level"), 9),
                                            -r.get("percent_used", 0)))
    return ordered[:limit], len(models)


def bar_fill(pct, width):
    """进度条填充像素；非零配额至少留 2px，避免 1% 看起来像 0。"""
    try:
        pct = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        return 0
    w = int(round(width * pct / 100.0))
    if pct > 0 and w < 2:
        w = 2
    return min(w, int(width))


def clamp_pos(x, y, w, h, work):
    """把窗口位置夹进工作区（work = (left, top, right, bottom)）。"""
    left, top, right, bottom = work
    max_x = max(left, right - w)
    max_y = max(top, bottom - h)
    return int(min(max(x, left), max_x)), int(min(max(y, top), max_y))


def default_pos(w, h, work, margin=16, bottom_gap=None):
    """默认贴右下角。

    bottom_gap 单独留出来，是为了让开输入法悬浮栏 —— 搜狗之类的小工具条
    常年挂在任务栏正上方右侧，正好压住这个位置。
    """
    left, _, right, bottom = work
    gap = margin if bottom_gap is None else bottom_gap
    return clamp_pos(right - w - margin, bottom - h - gap, w, h, work)


def scale_rect(rect, kx, ky=None):
    """按比例缩放一个矩形，用于把系统物理像素换算成 Tk 自己的坐标单位。"""
    ky = kx if ky is None else ky
    return (int(rect[0] * kx), int(rect[1] * ky),
            int(rect[2] * kx), int(rect[3] * ky))


def corner_offset(x, y, w, h, work):
    """窗口左上角 → 距工作区右下角的偏移。

    存偏移而不是绝对坐标：换缩放比例、换分辨率、改 DPI 感知方式之后，
    「贴在右下角再往上挪一点」这个意图不会漂。
    """
    return max(0, work[2] - (x + w)), max(0, work[3] - (y + h))


def pos_from_corner(dx, dy, w, h, work):
    x = work[2] - w - max(0, int(dx))
    y = work[3] - h - max(0, int(dy))
    return clamp_pos(x, y, w, h, work)


def is_wb_exe(name):
    """WorkBuddy.exe / WorkBuddyX.exe 都算；大小写与路径无关。"""
    base = os.path.basename(str(name or "")).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base.startswith(APP_MARK)


def app_state_from(wb_pids, window_pids):
    """进程集合 + 可见窗口所属 pid → 状态。纯函数，便于测试三种分支。"""
    if not wb_pids:
        return "gone"
    return "open" if (set(wb_pids) & set(window_pids)) else "hidden"


def row_texts(r):
    """一行模型要显示的三个文本：(名称, 百分比, 副标题)。

    副标题跟随 README 的判定优先级：先看是否已到上限，再看是否重置前耗尽，
    最后才给触限预测 —— 不能因为算出了 eta 就把「已用满」说成「还能撑 X 小时」。
    """
    pct = r.get("percent_used", 0)
    pct_txt = f"{pct:g}%"
    eta = r.get("eta_hours_to_limit")
    level = r.get("level")
    if level == "critical" and pct >= 95:
        sub = "已到上限，随时可能被拒"
    elif level == "critical" and eta is not None:
        sub = f"重置前耗尽 · 约 {qw.fmt_dur(eta)}后"
    elif eta is not None:
        sub = f"预计 {qw.fmt_dur(eta)}后触限"
    else:
        sub = r.get("verdict") or ""
    if r.get("shared_models"):
        sub += f" · 与 {len(r['shared_models'])} 个模型共用"
    return r.get("model", "unknown"), pct_txt, sub


def error_texts(msg):
    """把核心的长错误压成小窗能装下的两行，而不是硬截断。"""
    msg = msg or ""
    if "未找到配额快照" in msg:
        return "还没有配额快照", "跑一轮对话后会自动出现"
    if "读取失败" in msg:
        return "读取日志失败", elide_units(msg, 26)
    return "暂时读不到数据", elide_units(msg, 26)


def summary_lines(rep):
    """--print / 复制到剪贴板用的摘要，一行一个模型。

    判决语直接复用 row_texts，保证小窗和命令行不会各说各话。
    """
    if rep.get("error"):
        return ["配额胶囊：" + rep["error"]]
    out = []
    for r in rep.get("models") or []:
        name, pct, sub = row_texts(r)
        out.append(f"[{LEVEL_CN.get(r['level'], r['level'])}] {name} {pct} · {sub}")
    return out or ["配额胶囊：暂无数据"]


class Lifecycle:
    """跟随 WorkBuddy 的状态机：只吐四种动作，不碰窗口，可脱离 GUI 测试。

    feed(state, now) -> "wait" | "show" | "hide" | "exit"
      state: "open"（有可见主窗口）/ "hidden"（进程在但窗口关了）/ "gone"（进程没了）
    """

    ACTIONS = ("wait", "show", "hide", "exit")

    def __init__(self, follow=True, grace=300.0, hide_after=1.5, exit_after=10.0,
                 exit_on_close=False, stay=False, away_after=3.0, hide_on_away=True):
        self.follow = follow              # False = --always，忽略外部状态
        self.grace = grace                # 启动后等多久还没见到 WorkBuddy 就放弃
        self.hide_after = hide_after      # 窗口没了多久后隐藏小窗（关窗/退后台）
        self.exit_after = exit_after      # 进程没了多久后退出（容忍应用重启）
        self.exit_on_close = exit_on_close
        self.stay = stay                  # True = 应用退出也不结束，只隐藏，等它再开
        self.away_after = away_after      # 切到别的应用后多久让路（秒）
        self.hide_on_away = hide_on_away  # False = 别人在前台时也一直显示
        self.away_since = None
        self.t0 = None
        self.seen = False
        self.shown = False
        self.hidden_since = None
        self.gone_since = None

    def feed(self, state, now):
        if not self.follow:
            if not self.shown:
                self.shown = True
                return "show"
            return "keep"
        if self.t0 is None:
            self.t0 = now

        if state == "open":
            self.seen = True
            self.hidden_since = None
            self.gone_since = None
            self.away_since = None
            if not self.shown:
                self.shown = True
                return "show"
            return "keep"

        # WorkBuddy 还开着，但用户在别的应用里：留一段时间，久了才让路。
        # 直接隐藏会太急（复制个东西切回来，小窗就没了），所以用独立的
        # away_after 而不是窗口关闭用的 hide_after。
        if state == "away":
            self.seen = True
            self.gone_since = None
            self.hidden_since = None
            if not self.hide_on_away:
                if not self.shown:
                    self.shown = True
                    return "show"
                return "keep"
            if self.away_since is None:
                self.away_since = now
            if self.shown and now - self.away_since >= self.away_after:
                self.shown = False
                return "hide"
            return "keep" if self.shown else "wait"

        self.away_since = None
        if self.hidden_since is None:
            self.hidden_since = now
        if state == "gone":
            if self.gone_since is None:
                self.gone_since = now
            # 应用真的退出了：跟随模式下一段时间后收工（resident 模式只隐藏）
            if not self.stay and self.seen and self.gone_since is not None \
                    and now - self.gone_since >= self.exit_after:
                return "exit"
        else:
            self.gone_since = None

        if self.exit_on_close and state == "hidden" \
                and now - self.hidden_since >= self.hide_after:
            return "exit"

        if self.shown:
            if now - self.hidden_since >= self.hide_after:
                self.shown = False
                return "hide"
            return "keep"

        # 还没露过面：给 WorkBuddy 一个启动窗口期，超时就别留孤儿进程
        if not self.stay and not self.seen and self.grace > 0 \
                and now - self.t0 >= self.grace:
            return "exit"
        return "wait"

    def on_user_quit(self):
        self.shown = False


# ══════════════════════════════════════════════════════════════════════
# Windows 进程 / 窗口探测（ctypes，不拉 psutil，也不起子进程）
# ══════════════════════════════════════════════════════════════════════

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def win_processes():
    """[(pid, exe_name)]；非 Windows 或调用失败返回 []。"""
    if os.name != "nt":
        return []
    out = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snap in (0, -1, ctypes.c_void_p(-1).value):
            return []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out.append((int(entry.th32ProcessID), entry.szExeFile))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return out
    return out


_WB_PID_CACHE = {"pids": frozenset(), "ts": 0.0}
WB_RESCAN_EVERY = 20.0     # 秒：兜底全量重扫，防漏掉新起的 WorkBuddy 进程


def proc_exe(pid):
    """pid → exe 全路径；进程不在或无权限返回空串。单次约 0.05ms。"""
    if os.name != "nt":
        return ""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.QueryFullProcessImageNameW.argtypes = (
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong))
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def wb_process_ids(force=False, now=None):
    """当前 WorkBuddy 的 pid 集合 —— 带缓存，因为轮询每 1.5 秒就要问一次。

    全量枚举 300+ 进程约 10ms，而其中 99% 的时间里 WorkBuddy 的 pid 根本没变。
    所以先验证缓存里的每个 pid（`OpenProcess` + 查 exe 名，每个约 0.05ms），
    全部命中就直接返回；有一个失效、或距上次全扫超过 WB_RESCAN_EVERY 秒，
    才做一次全量扫描。

    为什么不能只看「pid 还活着」：Windows 会复用 pid，旧 pid 被别的进程接管后，
    光 OpenProcess 成功会把它误判成 WorkBuddy —— 必须再核对一次 exe 名。
    """
    now = time.time() if now is None else now
    cached = _WB_PID_CACHE["pids"]
    if (not force and cached and now - _WB_PID_CACHE["ts"] < WB_RESCAN_EVERY
            and all(is_wb_exe(proc_exe(p)) for p in cached)):
        return set(cached)
    pids = {pid for pid, name in win_processes() if is_wb_exe(name)}
    _WB_PID_CACHE["pids"] = frozenset(pids)
    _WB_PID_CACHE["ts"] = now
    return pids


def main_window_ok(visible, title, wide, high, ex_style, owner, iconic, cloaked,
                   min_w=200, min_h=150):
    """「像样的主窗口」判定 —— 辅助窗口一律不算。

    WorkBuddy 进程里常驻着一批 0×0 的小窗口（MSCTFIME UI / HintWnd /
    Sogou_TSF_UI），它们 IsWindowVisible=True 而且有标题。早期实现只检查
    「可见 + 有标题」，于是主窗口关掉之后仍然判成 open：
    小窗永不隐藏，还会以置顶身份浮在微信等其它应用上面 —— 用户看到的
    「打开微信时胶囊出现了」就是这个。
    """
    if not visible or not title:
        return False
    if iconic or cloaked:
        return False               # 最小化 / 被 DWM 隐藏（其它虚拟桌面）
    if owner:
        return False               # 有 owner 的都是附属窗口
    if ex_style & 0x00000080:      # WS_EX_TOOLWINDOW
        return False
    return wide >= min_w and high >= min_h


def visible_windows():
    """[(hwnd, title, pid)]：只要真正的顶层主窗口（判据见 main_window_ok）。"""
    if os.name != "nt":
        return []
    found = []
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            dwm = None

        class _R(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):
            try:
                n = u32.GetWindowTextLengthW(hwnd)
                title = ""
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u32.GetWindowTextW(hwnd, buf, n + 1)
                    title = buf.value
                r = _R()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                cloaked = 0
                if dwm is not None:
                    v = ctypes.c_int(0)
                    if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), 4) == 0:
                        cloaked = v.value
                if not main_window_ok(bool(u32.IsWindowVisible(hwnd)), title,
                                      r.right - r.left, r.bottom - r.top,
                                      u32.GetWindowLongW(hwnd, -20),
                                      u32.GetWindow(hwnd, 4),        # GW_OWNER
                                      bool(u32.IsIconic(hwnd)), bool(cloaked)):
                    return True
                pid = ctypes.c_ulong(0)
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((int(hwnd), title, int(pid.value)))
            except Exception:
                pass
            return True

        u32.EnumWindows(proto(_cb), 0)
    except Exception:
        return found
    return found


def foreground_pid():
    """当前前台窗口所属进程；取不到返回 0。"""
    if os.name != "nt":
        return 0
    try:
        u32 = ctypes.WinDLL("user32")
        h = u32.GetForegroundWindow()
        if not h:
            return 0
        pid = ctypes.c_ulong(0)
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def detect_state(self_pids=()):
    """'open' / 'away' / 'hidden' / 'gone'；异常一律 gone（宁可少显示，不胡乱弹窗）。

    away = WorkBuddy 主窗口还在，但前台是别的应用 —— 置顶小窗这时应该让路，
    否则它会浮在微信/浏览器上面。
    """
    try:
        wb = wb_process_ids()
    except Exception:
        return "gone"
    if not wb:
        return "gone"
    try:
        win_pids = {pid for _, _, pid in visible_windows()}
    except Exception:
        win_pids = set()
    state = app_state_from(wb, win_pids)
    if state != "open":
        return state
    fg = foreground_pid()
    if fg and fg not in wb and fg not in set(self_pids):
        return "away"
    return "open"


def work_area():
    """任务栏之外的可视区域，(left, top, right, bottom)。

    注意：返回的是**系统度量单位**，在 125%/150% 缩放下它可能与 Tk 的几何坐标
    不是同一个尺度（取决于进程的 DPI 感知级别）。要和 Tk 坐标混用，先过 tk_work_area()。
    """
    if os.name == "nt":
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = _RECT()
        try:
            if ctypes.WinDLL("user32").SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                return (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
    return (0, 0, 1920, 1080)


def system_screen_width():
    """系统度量下的屏幕宽（用于和 Tk 的 winfo_screenwidth 比出比例）。"""
    if os.name == "nt":
        try:
            w = ctypes.WinDLL("user32").GetSystemMetrics(0)
            if w:
                return int(w)
        except Exception:
            pass
    return None


def tk_work_area(tk_screen_w):
    """把工作区换算到 Tk 坐标单位。

    Tk 的 geometry 用的是它自己认的像素：进程不感知 DPI 时，Tk 宽 1707 而系统报 2560，
    两者混用会让「贴右下角」直接算到屏幕外。用两边屏幕宽的比值做一次换算即可对齐。
    """
    rect = work_area()
    sw, pw = tk_screen_w, system_screen_width()
    if sw and pw and pw != sw:
        k = sw / float(pw)
        return scale_rect(rect, k, k)
    return rect


def system_is_dark():
    """读系统「应用深色模式」开关，失败一律当浅色。"""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return int(val) == 0
    except Exception:
        return False


def dpi_scale():
    if os.name != "nt":
        return 1.0
    try:
        dpi = ctypes.WinDLL("user32").GetDpiForSystem()
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    try:
        dc = ctypes.WinDLL("user32").GetDC(0)
        dpi = ctypes.WinDLL("gdi32").GetDeviceCaps(dc, 88)  # LOGPIXELSX
        ctypes.WinDLL("user32").ReleaseDC(0, dc)
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def enable_dpi_awareness():
    if os.name != "nt":
        return
    for call in (lambda: ctypes.WinDLL("shcore").SetProcessDpiAwareness(1),
                 lambda: ctypes.WinDLL("user32").SetProcessDPIAware()):
        try:
            call()
            return
        except Exception:
            continue


# ══════════════════════════════════════════════════════════════════════
# 解释器 / 启动项 / 单实例
# ══════════════════════════════════════════════════════════════════════

def _py_launcher_paths():
    """从 `py -0p` 里问出本机所有注册过的解释器（PATH 里没有也能找到）。"""
    out = []
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(["py", "-0p"], capture_output=True, timeout=15,
                           creationflags=flags)
        text = (r.stdout or b"").decode("utf-8", "ignore")
        for line in text.splitlines():
            m = re.search(r"[A-Za-z]:\\", line)
            if m:
                p = line[m.start():].strip()
                if p.lower().endswith(".exe") and os.path.exists(p):
                    out.append(p)
    except Exception:
        return out
    return out


def python_candidates():
    """本机可能带 tkinter 的解释器：当前解释器优先，其次 py 启动器与常见安装目录。"""
    cands = []

    def add(p):
        if p and os.path.exists(p) and p not in cands:
            cands.append(p)

    exe = sys.executable or ""
    for n in ("pythonw.exe", "python.exe"):
        add(os.path.join(os.path.dirname(exe), n))
    add(exe)
    for n in ("pythonw.exe", "python3.exe", "python.exe"):
        add(shutil.which(n))
    for p in _py_launcher_paths():
        add(os.path.join(os.path.dirname(p), "pythonw.exe"))
        add(p)
    for pat in (r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe",
                r"%PROGRAMFILES%\Python3*\python.exe",
                r"%PROGRAMFILES(X86)%\Python3*\python.exe"):
        for p in sorted(glob.glob(os.path.expandvars(pat)), reverse=True):
            add(os.path.join(os.path.dirname(p), "pythonw.exe"))
            add(p)
    return cands


def _has_tkinter(exe):
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        r = subprocess.run([exe, "-c", "import tkinter"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=25, creationflags=flags)
        return r.returncode == 0
    except Exception:
        return False


def pick_python(prefer_windowless=True):
    """返回 (解释器路径, 是否带 tkinter)；找不到带 tkinter 的返回 (None, False)。

    先按「不带控制台的 pythonw」挑，退化时才用 python.exe。
    """
    fallback = None
    for exe in python_candidates():
        if not _has_tkinter(exe):
            continue
        windowless = os.path.basename(exe).lower().startswith("pythonw")
        if windowless and prefer_windowless:
            return exe, True
        if fallback is None:
            fallback = exe
    return (fallback, True) if fallback else (None, False)


def startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def startup_file():
    return os.path.join(startup_dir(), STARTUP_NAME)


def install_startup():
    """安装「看门狗」，而不是直接拉起小窗。

    直接拉起意味着 python 常驻（~90MB 工作集）。改成看门狗之后，常驻的只有
    ~3MB 的 WSH 解释器，它只观测 WorkBuddy 进程；WorkBuddy 一退出，小窗
    （跟随模式）自己就结束了 —— 这才是「随 WorkBuddy 起停」。

    脚本模板放在 tools/watchdog.tpl，安装时替换三个占位符。
    """
    exe, ok = pick_python(prefer_windowless=True)
    if not ok:
        return False, "找不到带 tkinter 的 Python，无法安装开机启动。"
    tpl = os.path.join(HERE, "tools", "watchdog.tpl")
    if not os.path.exists(tpl):
        return False, f"缺少看门狗模板：{tpl}"
    try:
        with open(tpl, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as exc:
        return False, f"读模板失败：{exc}"
    body = (body.replace("__HERE__", HERE)
                .replace("__PYW__", exe)
                .replace("__SUPPRESS__", SUPPRESS_FILE))
    try:
        os.makedirs(startup_dir(), exist_ok=True)
        # WSH 读脚本按 ANSI 解释，带 BOM 的 UTF-8 各版本表现不一，直接用系统代码页最稳
        try:
            fh = open(startup_file(), "w", encoding="mbcs", newline="")
        except LookupError:
            fh = open(startup_file(), "w", encoding="utf-8", newline="")
        with fh:
            fh.write(body)
        return True, f"已安装看门狗（随 WorkBuddy 起停）：{startup_file()}"
    except OSError as exc:
        return False, f"写入启动项失败：{exc}"


def uninstall_startup():
    p = startup_file()
    if os.path.exists(p):
        try:
            os.remove(p)
            return True, "已移除开机启动项。"
        except OSError as exc:
            return False, f"删除失败：{exc}"
    return True, "开机启动项本来就不存在。"


def startup_installed():
    return os.path.exists(startup_file())


def acquire_single_instance():
    """命名互斥体，多开时第二个直接退出。返回句柄（保持引用即可存活）。"""
    if os.name != "nt":
        return object()
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.CreateMutexW(None, False, "Local\\QuotaWatchTray")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return h
    except Exception:
        return object()


def spawn_detached(args=()):
    """用不带控制台的解释器把自己再拉起来（tray.bat 调这个）。"""
    exe, ok = pick_python(prefer_windowless=True)
    if not ok:
        log("找不到带 tkinter 的 Python。请用官方安装包装一个，或用 --which 查看。")
        return 2
    script = os.path.join(HERE, "quota_tray.py")
    flags = 0x00000008 | 0x08000000 if os.name == "nt" else 0  # DETACHED | NO_WINDOW
    try:
        subprocess.Popen([exe, script] + list(args), cwd=HERE, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return 0
    except Exception as exc:
        log(f"启动失败：{exc}")
        return 2


def spawn_after(delay=2.0, args=()):
    """延迟拉起一个新实例 —— 用于「重启小窗」。

    必须延迟：当前进程还占着 `Local\\QuotaWatchTray` 互斥体，新实例立刻起来会被
    判成「已在运行」然后退出。所以先起一个短命启动器睡几秒，再自己 quit 释放互斥体。
    """
    exe, ok = pick_python(prefer_windowless=True)
    if not ok:
        log("重启失败：找不到带 tkinter 的 Python。")
        return 2
    script = os.path.join(HERE, "quota_tray.py")
    code = (
        "import subprocess,sys,time;"
        "time.sleep(%r);"
        "subprocess.Popen([%r,%r]+sys.argv[1:],cwd=%r,"
        "creationflags=8|0x08000000,"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True)"
    ) % (delay, exe, script, HERE)
    flags = 0x00000008 | 0x08000000
    try:
        subprocess.Popen([exe, "-c", code] + list(args), cwd=HERE, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return 0
    except Exception as exc:
        log(f"重启失败：{exc}")
        return 2


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except OSError:
        pass


# ── 布局：用户点「保存布局」才固化，拖动本身只改内存 ───────────────────
# 存偏移（距工作区右下角）而不是绝对坐标：换分辨率/缩放后「贴右下角再往左挪一点」
# 这个意图不会漂。tray / report 用同一套坐标约定。

def layout_part(part, st=None):
    """取 layout.<part>；老版本 tray.json 没有 layout 键时回落到旧平铺字段。"""
    st = load_state() if st is None else st
    lay = st.get("layout")
    if isinstance(lay, dict):
        got = lay.get(part)
        if isinstance(got, dict):
            return got
    legacy = {"tray": ("dx", "dy"), "report": ("report_dx", "report_dy")}.get(part) or ()
    if legacy and all(isinstance(st.get(k), int) for k in legacy):
        return {"dx": st[legacy[0]], "dy": st[legacy[1]]}
    return {}


def save_layout(patch):
    """把若干 part 的偏移写进 layout。

    只清掉**本次写入的那个 part** 对应的旧平铺字段 —— 没写的 part 可能还在靠旧键，
    一起清会让另一个窗口的位置无声丢失（踩过：保存小窗把大窗的 report_dx 删了，
    大窗从此只能回默认落点，用户看到的是「位置锁死改不了」）。
    """
    st = load_state()
    lay = st.get("layout")
    if not isinstance(lay, dict):
        lay = {}
    legacy_keys = {"tray": ("dx", "dy"), "report": ("report_dx", "report_dy")}
    for part, val in patch.items():
        if not isinstance(val, dict):
            continue
        lay[part] = val
        for k in legacy_keys.get(part, ()):
            st.pop(k, None)
    lay["saved_at"] = time.strftime("%m-%d %H:%M")
    st["layout"] = lay
    save_state(st)
    return lay


def clear_layout():
    st = load_state()
    for k in ("layout", "dx", "dy", "report_dx", "report_dy"):
        st.pop(k, None)
    save_state(st)


# ══════════════════════════════════════════════════════════════════════
# 小窗
# ══════════════════════════════════════════════════════════════════════

def mix(c1, c2, t):
    """两个 #rrggbb 之间线性插值，t=0 取 c1。用于生成高光/淡底，Tk 没有 alpha。"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def grab_strip(x0, y, x1):
    """截屏幕一行像素，返回 [(r, g, b)]。失败返回 None。

    只截一行，开销远小于整屏截图；用来量输入框的边界。
    """
    if os.name != "nt":
        return None
    try:
        u32 = ctypes.WinDLL("user32")
        g32 = ctypes.WinDLL("gdi32")
        w = int(x1) - int(x0)
        if w <= 2:
            return None
        src = u32.GetDC(0)
        mem = g32.CreateCompatibleDC(src)
        bmp = g32.CreateCompatibleBitmap(src, w, 1)
        old = g32.SelectObject(mem, bmp)

        class _IH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        hdr = _IH()
        hdr.biSize = ctypes.sizeof(hdr)
        hdr.biWidth = w
        hdr.biHeight = -1
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        g32.BitBlt(mem, 0, 0, w, 1, src, int(x0), int(y), 0x40CC0020)  # SRCCOPY|CAPTUREBLT
        buf = ctypes.create_string_buffer(w * 4)
        g32.GetDIBits(mem, bmp, 0, 1, buf, ctypes.byref(hdr), 0)
        raw = buf.raw
        out = [(raw[i * 4 + 2], raw[i * 4 + 1], raw[i * 4]) for i in range(w)]

        g32.SelectObject(mem, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(0, src)
        return out
    except Exception:
        return None


def mask_rects(win_w, win_h, vis_w, vis_h):
    """把「只露右下角 vis_w×vis_h」表达成两个矩形（上条 + 左条）。

    两者并集 = 全集减去右下角那块。收起态的胶囊、展开动画的每一帧都靠它，
    算错一格就会漏出白边或吞掉胶囊角 —— 所以单独拿出来测。
    """
    vis_w = max(0, min(vis_w, win_w))
    vis_h = max(0, min(vis_h, win_h))
    top_h = win_h - vis_h
    rects = [(0, 0, win_w, top_h)]
    left_w = win_w - vis_w
    if left_w > 0:
        rects.append((0, top_h, left_w, win_h))
    return [r for r in rects if r[2] > r[0] and r[3] > r[1]]


def clamp_scroll(value, max_scroll):
    """滚动偏移夹取：负数当 0，超出上限取上限。"""
    if max_scroll <= 0:
        return 0
    return max(0, min(int(value), int(max_scroll)))


def round_rect_points(x0, y0, x1, y1, r, steps=5):
    pts = []
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    corners = ((x1 - r, y0 + r, -90, 0), (x1 - r, y1 - r, 0, 90),
               (x0 + r, y1 - r, 90, 180), (x0 + r, y0 + r, 180, 270))
    for cx, cy, a0, a1 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.extend([cx + r * math.cos(a), cy + r * math.sin(a)])
    return pts


class TrayWindow:
    CARD_W = 306      # 逻辑宽度：三行信息（名称/进度/判决）刚好舒展，又不挡视线
    PAD = 16
    HEAD_H = 38       # 标题带：比行区留更多呼吸感，详情/关闭按钮悬停需要圆形热区
    ROW_H = 56        # 一行三段（名称/进度/判决），段间距拉开后判决不再黏下一行
    FOOT_H = 30
    SHADOW = 5
    RADIUS = 13
    PILL_H = 32       # 收起态：一枚小胶囊，状态灯 + 最近模型 + 百分比
    PILL_MIN_W = 118
    RIGHT_RESERVE = 16  # 右缘避让：给宿主窗口的滚动条留出宽度（逻辑 px）
    BOTTOM_PIN = 34     # 底边钉在「输入框底边」上：本机实测输入框底距工作区底 34 逻辑px

    def __init__(self, args):
        import tkinter as tk
        from tkinter import font as tkfont
        self.tk = tk
        self.tkfont = tkfont
        self.args = args
        self.s = dpi_scale()
        self._hover_close = False
        self._hover_report = False
        self._pill_hover = False
        self._hover_job = None
        self._hover_row = -1
        self._dark = system_is_dark()
        self.pal = PALETTE["dark" if self._dark else "light"]
        saved = load_state()
        if isinstance(saved.get("topmost"), bool):
            args.topmost = saved["topmost"]

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("配额胶囊")
        # pythonw 下 sys.stderr 是 None：Tk 回调一抛异常，默认处理器写
        # traceback 时自己就会炸，整个进程跟着消失（无痕死亡）。统一改为
        # 落盘日志，异常不再致命。
        self.root.report_callback_exception = self._on_tk_exception
        try:
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", bool(args.topmost))
            self.root.attributes("-transparentcolor", C_KEY)
        except Exception:
            pass

        self.family = self._pick_family()
        self.mono = self._pick_mono()
        self.f = {
            "head": tkfont.Font(family=self.family, size=-self.px(13.5), weight="bold"),
            "name": tkfont.Font(family=self.family, size=-self.px(13.5)),
            "pct": tkfont.Font(family=self.mono, size=-self.px(15.5), weight="bold"),
            "sub": tkfont.Font(family=self.family, size=-self.px(11)),
            "foot": tkfont.Font(family=self.family, size=-self.px(10)),
            "close": tkfont.Font(family=self.family, size=-self.px(15)),
            "pill": tkfont.Font(family=self.mono, size=-self.px(13), weight="bold"),
            "pillname": tkfont.Font(family=self.family, size=-self.px(11.5)),
        }

        self.report = {"models": [], "error": "正在读取配额快照…"}
        self._report_ver = 0          # 报告版本号：_plan 的缓存键用它，别用 id()
        self._pending = None
        self._busy = False
        self._last_refresh = 0.0
        self._drag = None
        self._press = None
        self._toast = None
        self._theme_ts = 0.0
        self._pinned = bool(saved.get("pinned"))   # 右键可锁定常展开
        self._layout_dirty = False                 # 拖过但还没点「保存布局」
        self._avoid_input = saved.get("avoid_input", True) is not False
        self._show_when_away = saved.get("always_on_top_show") is True
        # 右键菜单复用同一个 Menu，配一组常驻 BooleanVar（每次右键重建会累积 Tcl 变量）
        self._menu = None
        self.var_top = tk.BooleanVar(self.root, value=bool(args.topmost))
        self.var_away = tk.BooleanVar(self.root, value=not self._show_when_away)
        self.var_avoid = tk.BooleanVar(self.root, value=self._avoid_input)
        self.var_pin = tk.BooleanVar(self.root, value=self._pinned)
        self.var_boot = tk.BooleanVar(self.root, value=False)
        try:
            self._edge_cache = int(saved.get("input_edge") or 0)
        except (TypeError, ValueError):
            self._edge_cache = 0
        # 启动即收起（除非用户锁定展开）：先不打扰，点了才看
        self._collapsed = not self._pinned
        self._anchor = None          # 窗口右下角，形态切换的基准
        self._collapse_job = None
        self._report_win = None
        self._layout()

        self.canvas = tk.Canvas(self.root, bg=C_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)

        self.life = Lifecycle(follow=not args.always, grace=args.grace,
                              exit_on_close=args.exit_on_close, stay=args.resident,
                              hide_on_away=not (saved.get("always_on_top_show") is True))
        self._start_watch()
        self.root.after(60, self._poll)
        self.refresh()

    # ── 尺寸与定位 ────────────────────────────────────────────────
    def px(self, v):
        return max(1, int(round(v * self.s)))

    def _pick_family(self):
        try:
            fams = set(self.tkfont.families())
        except Exception:
            fams = set()
        for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"):
            if f in fams:
                return f
        return "TkDefaultFont"

    def _elide(self, font, text, max_px):
        """按像素精确省略：比按字符宽度估算省空间，长模型名能多显示几个字。"""
        try:
            if max_px <= 0:
                return ""
            if font.measure(text) <= max_px:
                return text
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid] + "…") <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            return (text[:lo] + "…") if lo > 0 else "…"
        except Exception:
            return elide_units(text, max(1, int(max_px / (6.6 * self.s))))

    def _pick_mono(self):
        """百分比用等宽字体：数字等宽，跳动时不会左右抖。"""
        try:
            fams = set(self.tkfont.families())
        except Exception:
            fams = set()
        for f in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"):
            if f in fams:
                return f
        return self.family

    def _probe_edge(self):
        """量输入框右边缘的屏幕 x 坐标；量不到返回 None。

        做法：在胶囊中线高度附近取三条横线，逐列统计「与背景色显著不同」
        出现了几次，从右往左找第一条**在多行上重复出现**的竖线 —— 那就是
        输入框的右边框。

        为什么不是单行扫描：单行像素分不清「边框」和「框内文字/图标」，
        两者都只是「和背景不同」。而边框是竖的，在多行上位置一致，文字
        笔画做不到 —— 这一条判据不依赖主题明暗。

        早期版本假设「边框左侧 26px 是实心区域」，浅色主题下输入框是白底
        白字 + 一条细灰边，这条恒不成立，于是校准永远量不到，程序一直沿用
        早前误测的假值（issue #1）。现在不再要求实心，只要「竖线」即可。

        另一条硬约束：结果必须落在自己窗口左缘的左侧。窗口是盖在宿主界面
        上的，输入框右缘不可能跑到窗口右边去；不挡的话会把自身的投影/边角
        量成边框（表现为卡片越校准越窄）。
        """
        if not self._avoid_input or os.name != "nt":
            return None
        try:
            wa = self.wa()
            # 窗口恒为展开尺寸，胶囊贴在右下角：扫描线按胶囊实际位置取
            _, ph = self._pill_size()
            sh2 = self.px(self.SHADOW) * 2
            y = self.origin[1] + self.win_h - sh2 - ph // 2
            # 扫描带右端停在窗口左缘之前。窗口是盖在宿主界面上的，采样点一旦
            # 落进自己的窗口，背景色、特征列都会变成自己的像素 —— 早期版本就是
            # 这样把自己的边角量成了输入框边框。
            x_end = int(min(self.origin[0] - self.px(4), wa[2] - self.px(14)))
            x_start = int(max(wa[0] + self.px(40), x_end - self.px(720)))
            x_start = int(max(wa[0] + self.px(40), x_end - self.px(720)))
            if x_end - x_start < self.px(160):
                return None

            span = max(self.px(6), ph // 4)
            rows = []
            for dy in (-span, 0, span):
                strip = grab_strip(x_start, int(y + dy), x_end)
                if strip and len(strip) >= 40:
                    rows.append(strip)
            if len(rows) < 2:
                return None            # 抓不到像素（息屏 / 非 Windows / 被遮挡）

            def far(c, b):
                return abs(c[0] - b[0]) + abs(c[1] - b[1]) + abs(c[2] - b[2])

            width = min(len(r) for r in rows)
            bg = [r[-1] for r in rows]
            hits = []
            for j in range(width):
                # 阈值 14：浅色主题下「白卡片 255」与「页面底色 251」只差 12，
                # 门槛放宽会被算成特征，右边框就会被误判到卡片内部。
                hits.append(sum(1 for r, b in zip(rows, bg) if far(r[j], b) > 14))
            if max(hits) == 0:
                return None            # 整片同色（纯色桌面 / 空白画布），读了也没用

            need = 2 if len(rows) >= 3 else 1
            for j in range(width - 2, -1, -1):
                if hits[j] < need:
                    continue
                edge = x_start + j
                if edge <= wa[0] + self.px(80):
                    return None        # 已经扫到工作区左侧，再左没有意义
                return edge
            return None
        except Exception:
            return None

    def _input_edge(self):
        """输入框右边缘：只在收起态量，量到就落盘，之后一直复用。

        展开态**绝不现场读屏**：卡片本身就压在扫描线上，读回来的「右缘」是自己
        卡片的左缘 —— 这个值偏小，会让卡片下次往左长得更宽、直接压住输入框。
        读屏也可能失败（息屏、全黑、被别的窗口盖住），所以量到的好结果必须存下来。
        截一行像素约 12ms，加 5 秒节流；真要重测走右键「重新校准位置」。
        """
        cached = int(getattr(self, "_edge_cache", 0) or 0)
        if not self._collapsed:
            return cached or None
        if time.time() - getattr(self, "_edge_ts", 0.0) < 5.0:
            return cached or None
        self._edge_ts = time.time()
        edge = self._probe_edge()
        if edge and abs(edge - cached) > 2:
            self._edge_cache = edge
            try:
                st = load_state()
                st["input_edge"] = edge
                save_state(st)
            except Exception:
                pass
            return edge
        return cached or None

    def _plan(self, as_expanded=None):
        """把「卡片多宽、放几行、底边落在哪」一次算清。

        关键事实：WorkBuddy 的输入框是一块有限宽的圆角卡片（本机 x 832~2148），
        右边留着一整条空档。所以「不压住」正解是**横向**把卡片塞进这条空档，
        而不是纵向把卡片整体抬走。

          · 收起           → 胶囊尺寸，位置不动
          · 右侧有空档      → 卡片整个塞进空档，底边不动，原地向上长（首选）
          · 空档不够/量不到  → 底边抬到输入框上沿之上，宁可位移也不压住（兜底）

        as_expanded：缺省跟随当前收起/展开状态；显式传 True 强制按展开卡片
        口径算 —— 窗口尺寸恒为展开尺寸（见 _apply_state_pos），收起态也要用它。
        """
        if as_expanded is None:
            as_expanded = not self._collapsed
        wa = self.wa()
        rows_all, total = pick_rows(self.report, 999)
        ax, ay = self._anchor or (self.origin[0] + self.win_w, self.origin[1] + self.win_h)
        key = (as_expanded, self._report_ver, ax, ay, len(rows_all), self._avoid_input)
        cache = getattr(self, "_plan_cache", None)
        if cache and cache[0] == key:
            return cache[1]

        if not as_expanded:
            w, h = self._pill_size()
            plan = {"w": w, "h": h, "rows": [], "total": total,
                    "bottom": ay, "mode": "pill"}
        else:
            gap = self.px(16)
            max_w = self.px(self.CARD_W)
            chrome = self.px(self.HEAD_H + self.FOOT_H + self.SHADOW * 2)
            edge = self._input_edge() if self._avoid_input else None
            room = (ax - edge - gap) if edge else 0
            if not self._avoid_input:
                w, bottom, mode = max_w, ay, "free"
            elif edge and room >= self.px(196):
                # 量到了输入框右边缘，且空档够宽：整张卡片塞进空档，底边不动
                w, bottom, mode = min(max_w, room), ay, "gutter"
            else:
                # 量不到 / 空档不够：只允许「涨高不涨宽」——
                # 宽度沿用胶囊（这一列已经证明不压输入框），所以再怎么长也不会压过去
                w = self._pill_size()[0]
                bottom, mode = ay, "column"
            avail = bottom - wa[1] - self.px(12) - chrome
            fit = max(1, int(avail // self.px(self.ROW_H)))
            fit = min(fit, max(1, int(getattr(self.args, "max_rows", 3) or 3)))
            rows = rows_all[:max(1, min(fit, len(rows_all)))] if rows_all else []
            h = (self.px(self.HEAD_H) + self.px(self.ROW_H) * max(1, len(rows))
                 + self.px(self.FOOT_H))
            plan = {"w": w, "h": h, "rows": rows, "total": total,
                    "bottom": bottom, "mode": mode}

        self._plan_cache = (key, plan)
        return plan

    def _visible_rows(self):
        plan = self._plan()
        return plan["rows"], plan["total"]

    def _n_rows(self):
        if self._collapsed:
            return 1
        return max(1, len(self._plan()["rows"]))

    def _pill_content(self):
        """胶囊内容：最近一次对话用的模型 + 它的百分比。"""
        row = latest_row(self.report)
        if row:
            name = short_model(row.get("model"))
            pct = f"{row.get('percent_used', 0):g}%"
        else:
            name, pct = "配额胶囊", "—"
        try:
            name = self._elide(self.f["pillname"], name, self.px(86))
        except Exception:
            pass
        return name, pct

    def _pill_size(self):
        name, pct = self._pill_content()
        try:
            nw = self.f["pillname"].measure(name)
            pw = self.f["pill"].measure(pct)
        except Exception:
            nw, pw = self.px(80), self.px(32)
        w = self.px(12) + self.px(7) + self.px(8) + nw + self.px(8) + pw + self.px(12)
        return max(self.px(self.PILL_MIN_W), w), self.px(self.PILL_H)

    def _card_size(self):
        plan = self._plan()
        return plan["w"], plan["h"]

    def _layout(self):
        # 首次布局：窗口恒按展开卡片的静态尺寸创建（收起态只是视觉上被遮罩
        # 盖住、只露右下角胶囊）。之后窗口尺寸不再变化 —— 分层窗口 resize
        # 会闪黑，见 _apply_state_pos。
        w = self.px(self.CARD_W)
        h = self.px(self.HEAD_H + self.FOOT_H + self.ROW_H * max(1, self.args.max_rows))
        sh = self.px(self.SHADOW)
        win_w, win_h = w + sh * 2, h + sh * 2
        wa = self.wa()
        st = load_state()
        lay = layout_part("tray", st)
        dx, dy = lay.get("dx"), lay.get("dy")
        if isinstance(dx, int) and isinstance(dy, int):
            x, y = pos_from_corner(dx, dy, win_w, win_h, wa)
        else:
            x, y = default_pos(win_w, win_h, wa, margin=self.px(8),
                               bottom_gap=self.px(self.BOTTOM_PIN))
        self.win_w, self.win_h = win_w, win_h
        self.origin = (x, y)
        self._anchor = (x + win_w, y + win_h)
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        # 统一走 _apply_state_pos：把窗口校正成真实展开尺寸、摆好锚点
        self._apply_state_pos()

    def wa(self):
        """工作区，已换算成 Tk 坐标单位，几何计算只认这个。"""
        return tk_work_area(self.root.winfo_screenwidth())

    def _set_collapsed(self, flag, animate=False):
        if flag == self._collapsed:
            return
        self._collapsed = flag
        if animate:
            if not flag:
                # 展开：先画好完整卡片，窗口从胶囊大小显示卡片左上角，
                # 动画期间「长」到全尺寸 —— 胶囊原地变成卡片，不从底下弹
                self._safe_render()
                self._apply_state_pos(animate=True)
            else:
                # 收起：动画期间继续显示卡片内容缩回锚点，结束时才换胶囊
                self._apply_state_pos(animate=True, render_at_end=True)
        else:
            self._apply_state_pos()
            self._safe_render()

    def _apply_state_pos(self, animate=False, render_at_end=False):
        """按计划摆位置：窗口尺寸**恒为展开卡片的尺寸**，右边缘钉在锚点上。

        卡片横向被限制在输入框右侧的空档里，所以底边可以完全不挪 —— 展开就是
        从胶囊原地往左上方长；只有空档不够时才整体上抬（兜底）。

        为什么窗口恒定：这是分层窗口（-transparentcolor），任何 resize 都可能
        让未重绘区被系统涂黑。所以收起态不缩窗 —— 胶囊画在窗口右下角（锚点处），
        其余区域由透明遮罩盖住；展开/收起动画只是遮罩插值（_grow_mask），
        物理上不存在重排，闪黑无从发生。
        """
        if self._anchor is None:
            self._anchor = (self.origin[0] + self.win_w,
                            self.origin[1] + self.win_h)
        # 锚点 y 不再强制钉在输入框底线上：用户可以把小窗拖到任意高度，
        # 位置由 _place 负责夹进工作区。默认落点仍在 BOTTOM_PIN 那条线上
        # （见 _layout），只是不再每拍把小窗拽回去。
        plan = self._plan()                       # 视觉尺寸（胶囊或卡片）
        exp = self._plan(as_expanded=True)        # 窗口尺寸，恒为展开口径
        sh2 = self.px(self.SHADOW) * 2
        prev_vis = getattr(self, "_vis_size", None) or \
            (plan["w"] + sh2, plan["h"] + sh2)
        self._vis_size = (plan["w"] + sh2, plan["h"] + sh2)
        self.win_w, self.win_h = exp["w"] + sh2, exp["h"] + sh2
        before = self._anchor
        self._place(self._anchor[0] - self.win_w, self._anchor[1] - self.win_h)
        if self._anchor != before:
            # 摆位被夹取/避让改写：锚点已在 _place 里同步，但刚才的卡片宽度
            # 是按旧锚点算的 —— 作废缓存重算一次，否则左缘会探进输入框
            self._plan_cache = None
            exp = self._plan(as_expanded=True)
            if exp["w"] + sh2 != self.win_w or exp["h"] + sh2 != self.win_h:
                self.win_w, self.win_h = exp["w"] + sh2, exp["h"] + sh2
                self._place(self._anchor[0] - self.win_w,
                            self._anchor[1] - self.win_h)
        if animate:
            self._grow_mask(prev_vis, self._vis_size, render_at_end)

    def _grow_mask(self, from_size, final_size, render_at_end, steps=8, delay=14):
        """展开/收起动画：窗口尺寸与位置全程不动，用透明色遮罩把可见区
        （锚定右下角 = 胶囊的位置）从 from_size 插值到 final_size。

        可见区钉在右下角，所以视觉上是「从胶囊原地长出来 / 缩回胶囊里」，
        且全程零重排 —— 不闪黑。
        """
        fw, fh = final_size
        sw, sh = from_size
        token = getattr(self, "_slide_token", 0) + 1
        self._slide_token = token            # 快速连点时只让最后一次动画生效

        def step(i):
            try:
                if token != self._slide_token:
                    return
                t = i / float(steps)
                t = 1 - (1 - t) ** 2
                w = max(1, int(sw + (fw - sw) * t))
                h = max(1, int(sh + (fh - sh) * t))
                self._paint_grow_mask(w, h)
                if i < steps:
                    self.root.after(delay, lambda: step(i + 1))
                else:
                    if render_at_end:
                        self._safe_render()      # 换胶囊内容 + 常驻遮罩
                    else:
                        self.canvas.delete("growmask")
            except Exception:
                # 动画链断了也必须把遮罩清干净，否则卡片上留一块透明豁口
                import traceback
                log("grow_mask 异常：\n" + traceback.format_exc())
                try:
                    self.canvas.delete("growmask")
                except Exception:
                    pass

        step(1)

    def _paint_grow_mask(self, w_i, h_i):
        """把可见区（右下角 w_i × h_i）外的一切盖成透明色键。"""
        c = self.canvas
        c.delete("growmask")
        for x0, y0, x1, y1 in mask_rects(self.win_w, self.win_h, w_i, h_i):
            c.create_polygon(x0, y0, x1, y0, x1, y1, x0, y1,
                             fill=C_KEY, outline=C_KEY, tags="growmask")

    def _place(self, x, y):
        """所有摆位都走这里：夹进工作区，并且不让窗口压进输入区。

        胶囊和展开卡片共用这条规则，所以两者的底边天然对齐 —— 展开就是原地长高，
        不会从输入框里「跳」到上面去。
        右缘额外让出 RIGHT_RESERVE：宿主窗口的滚动条贴着屏幕右缘，
        胶囊/卡片压在它上面会一直若隐若现地硌眼。
        夹取/避让改写了位置就必须同步锚点 —— 布局引擎按锚点算卡片宽度，
        锚点和实际窗口右缘差几px，卡片左缘就探进输入框几px（踩过）。
        """
        wa = self.wa()
        x, y = clamp_pos(x, y, self.win_w, self.win_h, wa)
        x = max(wa[0], min(x, wa[2] - self.win_w - self.px(self.RIGHT_RESERVE)))
        self.origin = (x, y)
        self._anchor = (x + self.win_w, y + self.win_h)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

    # ── 绘制 ─────────────────────────────────────────────────────
    def render(self):
        c, p = self.canvas, self.pal
        c.delete("all")
        if self._collapsed:
            self._render_pill()
            return
        sh = self.px(self.SHADOW)
        w, h = self._card_size()
        x0, y0, x1, y1 = sh, sh, sh + w, sh + h
        rows, total = self._visible_rows()
        rad = self.px(self.RADIUS)

        # 影子：三层由外到内递深，Tk 不能做模糊，靠层数堆出柔和感
        for i, col in zip((3, 2, 1), p["shadow"]):
            o = self.px(i)
            c.create_polygon(round_rect_points(x0 - o, y0 - o, x1 + o, y1 + o, rad + o),
                             fill=col, outline="")
        c.create_polygon(round_rect_points(x0, y0, x1, y1, rad),
                         fill=p["card"], outline=p["edge"])

        pad = self.px(self.PAD)
        cy = y0 + self.px(self.HEAD_H) // 2

        c.create_text(x0 + pad, cy, anchor="w", text="配额胶囊",
                      font=self.f["head"], fill=p["text"])

        gen = (self.report.get("generated_at") or "").split(" ")[-1]
        box = (x1 - pad - self.px(24), y0 + self.px(10),
               x1 - pad + self.px(8), y0 + self.px(32))
        self._close_box = box
        # 窄卡（column 模式）：详情 chip 和时间戳放不下，只留 ×，完整报告走右键/点行
        narrow = w < self.px(160)
        self._report_box = None if narrow else \
            (box[0] - self.px(48), box[1], box[0] - self.px(10), box[3])
        if gen and not narrow:
            c.create_text(self._report_box[0] - self.px(10), cy, anchor="e", text=gen,
                          font=self.f["foot"], fill=mix(p["sub"], p["card"], 0.20))
        if not narrow:
            # 「详情」是一枚常驻细边框的圆角按钮，悬停填色 —— 不再是裸文本
            c.create_polygon(round_rect_points(self._report_box[0], self._report_box[1],
                                               self._report_box[2], self._report_box[3],
                                               self.px(8)),
                             fill=p["hover"] if self._hover_report else p["card"],
                             outline=p["edge"])
            c.create_text((self._report_box[0] + self._report_box[2]) / 2, cy, text="详情",
                          font=self.f["foot"],
                          fill=p["text"] if self._hover_report else p["sub"])
        if self._hover_close:
            c.create_oval(box[0] + self.px(4), box[1] + self.px(3),
                          box[2] - self.px(2), box[3] - self.px(3),
                          fill=p["close_hover_bg"], outline="")
        c.create_text((box[0] + box[2]) / 2 + self.px(1), cy, anchor="center", text="×",
                      font=self.f["close"],
                      fill=p["close_hover"] if self._hover_close else p["sub"])

        c.create_line(x0 + pad, y0 + self.px(self.HEAD_H),
                      x1 - pad, y0 + self.px(self.HEAD_H), fill=p["edge_soft"])

        top = y0 + self.px(self.HEAD_H)
        if self.report.get("error"):
            # 空态/错误态：居中的一枚安静小表盘 + 两行说明；卡片矮时退化为纯文字居中
            t1, t2 = error_texts(self.report["error"])
            midx = (x0 + x1) / 2
            body_bottom = y1 - self.px(self.FOOT_H)
            bh_ = max(0, body_bottom - top)
            if bh_ >= self.px(112):
                midy = top + bh_ / 2 - self.px(16)
                rg = self.px(21)
                c.create_arc(midx - rg, midy - rg, midx + rg, midy + rg,
                             start=25, extent=220, style="arc",
                             outline=p["track"], width=self.px(3.5))
                c.create_line(midx, midy, midx + rg * 0.60, midy - rg * 0.60,
                              fill=p["sub"], width=self.px(2))
                c.create_oval(midx - self.px(2.5), midy - self.px(2.5),
                              midx + self.px(2.5), midy + self.px(2.5),
                              fill=p["sub"], outline="")
                ty1, ty2 = midy + rg + self.px(20), midy + rg + self.px(40)
            else:
                ty1, ty2 = top + bh_ * 0.38, top + bh_ * 0.72
            text_w = w - pad * 2
            c.create_text(midx, ty1, text=self._elide(self.f["name"], t1, text_w),
                          font=self.f["name"], fill=p["text"])
            c.create_text(midx, ty2, text=self._elide(self.f["sub"], t2, text_w),
                          font=self.f["sub"], fill=p["sub"])
        elif not rows:
            # 本周期一个都没用过（全是上一周期的残值）：别摆一张空卡。
            # 窄卡（column 兜底模式）只放得下一句短的，别硬塞整句再被裁掉。
            stale = self.report.get("stale_models") or 0
            midx = (x0 + x1) / 2
            body_bottom = y1 - self.px(self.FOOT_H)
            midy = top + max(0, body_bottom - top) / 2
            text_w = w - pad * 2
            narrow_empty = text_w < self.f["name"].measure("本周期还没有使用记录")
            t1 = "本周期未使用" if narrow_empty else "本周期还没有使用记录"
            c.create_text(midx, midy - (self.px(4) if narrow_empty else self.px(6)),
                          text=self._elide(self.f["name"], t1, text_w),
                          font=self.f["name"], fill=p["text"])
            if stale and not narrow_empty:
                c.create_text(midx, midy + self.px(16),
                              text=self._elide(self.f["sub"],
                                               f"{stale} 个模型停在上一周期，重置后未使用",
                                               text_w),
                              font=self.f["sub"], fill=p["sub"])
        else:
            row_top = top
            for i, r in enumerate(rows):
                if i > 0:                # 行间发丝线：判决语和下一行模型名不再黏成一片
                    c.create_line(x0 + pad + self.px(2), row_top,
                                  x1 - pad - self.px(2), row_top, fill=p["edge"])
                if i == self._hover_row:
                    c.create_polygon(
                        round_rect_points(x0 + self.px(7), row_top + self.px(4),
                                          x1 - self.px(7),
                                          row_top + self.px(self.ROW_H) - self.px(4),
                                          self.px(9)),
                        fill=p["hover"], outline="")
                self._draw_row(c, r, x0 + pad, x1 - pad, row_top)
                row_top += self.px(self.ROW_H)

        fy = y1 - self.px(self.FOOT_H)
        c.create_line(x0 + pad, fy, x1 - pad, fy, fill=p["edge_soft"])
        ty = fy + self.px(self.FOOT_H) // 2
        fresh = mix(p["sub"], p["card"], 0.30)     # 页脚刻意压淡，不与正文抢注意力
        if self.report.get("error"):
            foot = "暂无快照"
        else:
            reset = (rows[0].get("reset_local") or "").split(" ")[-1] if rows else ""
            foot = (f"重置 {reset} · " if reset else "") + f"{self.report.get('records', 0)} 条快照"
        foot = self._elide(self.f["foot"], foot, (x1 - pad) - (x0 + pad))
        c.create_text(x0 + pad, ty, anchor="w", text=foot, font=self.f["foot"], fill=fresh)
        note = note_col = None
        if self._toast and time.time() < self._toast[1]:
            note, note_col = self._toast[0], p["toast"]
        elif not self.report.get("error"):
            stale = self.report.get("stale_models") or 0
            parts = []
            if total:
                parts.append(f"共 {total} 个" +
                             (f" · 还有 {total - len(rows)}" if total > len(rows) else ""))
            if stale:
                parts.append(f"{stale} 个未用")
            note = " · ".join(parts) or None
            note_col = fresh
        if note:
            # 窄卡（column 模式）放不下左右两段：右侧注释直接省略，绝不叠字
            room = (x1 - pad) - (x0 + pad) - self.f["foot"].measure(foot) - self.px(10)
            if self.f["foot"].measure(note) <= room:
                c.create_text(x1 - pad, ty, anchor="e", text=note,
                              font=self.f["foot"], fill=note_col)

    def _render_pill(self):
        """收起态：一枚胶囊，只有状态灯和百分比，占位极小。

        灯色取**全场最危险**档位而不是最近模型的档位 —— 本工具的职责是
        「告诉你哪个先撞墙」：最近用的模型哪怕闲着，只要有模型快到上限，
        收起态也必须亮红。文字仍显示最近对话的模型 + 它自己的百分比。

        窗口恒为展开卡片尺寸：胶囊画在右下角锚点处，其余区域交给
        _safe_render 里的常驻遮罩盖成透明。
        """
        c, p = self.canvas, self.pal
        w, h = self._card_size()               # 胶囊内容尺寸（不含影子）
        sh = self.px(self.SHADOW)
        dx = max(0, self.win_w - w - sh * 2)   # 大窗口里贴到右下角
        dy = max(0, self.win_h - h - sh * 2)
        x0, y0, x1, y1 = sh + dx, sh + dy, sh + dx + w, sh + dy + h
        rows_all, _ = pick_rows(self.report, 999)
        accent = p["level"].get(worst_level(rows_all), p["sub"])   # 灯：全场最危险
        row = latest_row(self.report)
        own = p["level"].get((row or {}).get("level"), p["sub"])   # 文字：它自己的档位
        r = (y1 - y0) / 2.0

        c.create_polygon(round_rect_points(x0, y0 + self.px(1), x1, y1 + self.px(1), r),
                         fill=p["shadow"][0], outline="")
        c.create_polygon(round_rect_points(x0, y0, x1, y1, r),
                         fill=mix(p["card"], p["hover"], 1.0 if self._pill_hover else 0.0),
                         outline=mix(p["edge"], p["sub"], 0.35 if self._pill_hover else 0.0))
        self._close_box = None          # 收起态没有关闭按钮，别让点击误触

        cy = (y0 + y1) / 2.0
        dot = self.px(3.0)
        dx_ = x0 + self.px(12) + dot
        halo = self.px(4.6 if self._busy else 4.0)
        c.create_oval(dx_ - halo, cy - halo, dx_ + halo, cy + halo,
                      fill=mix(accent, p["card"], 0.72 if self._dark else 0.82),
                      outline="")
        c.create_oval(dx_ - dot, cy - dot, dx_ + dot, cy + dot, fill=accent, outline="")
        name, pct = self._pill_content()
        c.create_text(dx_ + dot + self.px(8), cy, anchor="w", text=name,
                      font=self.f["pillname"], fill=p["text"])
        c.create_text(x1 - self.px(12), cy, anchor="e", text=pct,
                      font=self.f["pill"], fill=own)

    def _draw_row(self, c, r, xl, xr, top):
        """一行 = 名称/百分比（同一基线）+ 进度条 + 判决，三层信息密度递进。

        ROW_H 56 下三段的 y 节奏：名称 13 → 进度条 29 → 判决 46，
        判决与下一行发丝线之间留出 10px，不再视觉粘连。
        """
        p = self.pal
        accent = p["level"].get(r.get("level"), p["sub"])
        name, pct, sub = row_texts(r)

        # 名字吃满「总宽 - 百分比实际宽度 - 间距」，而不是预留固定值
        pct_w = self.f["pct"].measure(pct)
        name_max = (xr - xl) - pct_w - self.px(12)
        c.create_text(xl, top + self.px(13), anchor="w",
                      text=self._elide(self.f["name"], name, name_max),
                      font=self.f["name"], fill=p["text"])
        # 百分比字号更大，中心下移 2px 才能和名字视觉对齐基线
        c.create_text(xr, top + self.px(14), anchor="e", text=pct,
                      font=self.f["pct"], fill=accent)

        by = top + self.px(29)
        bh = self.px(5)
        bw = xr - xl
        c.create_polygon(round_rect_points(xl, by, xr, by + bh, bh / 2.0),
                         fill=p["track"], outline="")
        fw = bar_fill(r.get("percent_used", 0), bw)
        if fw > 0:
            c.create_polygon(round_rect_points(xl, by, xl + fw, by + bh, bh / 2.0),
                             fill=accent, outline="")
            # 顶部一道高光，让填充看起来有厚度（Tk 没有 alpha，用混色模拟）
            c.create_polygon(
                round_rect_points(xl + self.px(1), by + self.px(1),
                                  max(xl + self.px(2), xl + fw - self.px(1)),
                                  by + self.px(2.6), self.px(1.3)),
                fill=mix(accent, "#ffffff", 0.26), outline="")

        if sub:
            c.create_text(xl, top + self.px(46), anchor="w",
                          text=self._elide(self.f["sub"], sub, bw),
                          font=self.f["sub"], fill=p["sub"])

    # ── 数据刷新 ─────────────────────────────────────────────────
    def refresh(self):
        """全量扫日志是几百毫秒量级（本机 58 个文件 / 76MB 实测 660ms），
        放后台线程，界面不卡。耗时随日志累计线性增长，别假设它恒定。"""
        if self._busy:
            return
        self._busy = True
        self._last_refresh = time.time()
        threading.Thread(target=self._scan, daemon=True).start()
        self.root.after(100, self._drain)

    def _scan(self):
        try:
            rep = qw.build_report(self.args.window, self.args.warn)
        except Exception as exc:  # 读取失败不能拖垮小窗
            rep = {"models": [], "error": f"读取失败：{exc}"}
        self._pending = rep
        self._busy = False

    def _drain(self, tries=0):
        """把后台结果搬进界面。只在真有任务时自我调度，空闲时彻底不唤醒。"""
        try:
            rep, self._pending = self._pending, None
            if rep is not None:
                self.report = rep
                self._report_ver += 1
                if self._size_changed():      # 宽度也会变（胶囊要装下模型名），两边都要看
                    self._apply_state_pos()
                self._safe_render()
                return
            if self._busy and tries < 300:  # 最多等 30 秒，避免线程异常时无限空转
                self.root.after(100, lambda: self._drain(tries + 1))
        except Exception:
            import traceback
            log("drain 异常：\n" + traceback.format_exc())

    def _size_changed(self):
        # 窗口恒为展开尺寸：与展开口径的 plan 比，而不是视觉尺寸
        exp = self._plan(as_expanded=True)
        sh2 = self.px(self.SHADOW) * 2
        return (abs(exp["w"] + sh2 - self.win_w) > 1) or \
               (abs(exp["h"] + sh2 - self.win_h) > 1)

    def _on_tk_exception(self, exc, val, tb):
        """Tk 回调异常统一落盘，绝不再往 stderr 写（pythonw 下它是 None，
        默认处理器自己会炸、带崩进程）。小窗照常活着，事后看 tray.log。"""
        import traceback
        log("Tk 回调异常：\n" + "".join(traceback.format_exception(exc, val, tb)))

    def _safe_render(self):
        """渲染异常不能打断 after 回调链，否则整个小窗就哑了。

        收起态渲染完再补常驻遮罩：窗口恒为展开尺寸，只露右下角的胶囊。
        展开态则必须清掉一切 growmask 残留 —— 动画遮罩只要残留一块，
        卡片上就是一块透明豁口（用户截到过）。渲染异常一律落盘，
        半截画面 + 无日志的无痕故障没法查。
        """
        try:
            self.render()
            if self._collapsed:
                p = self._plan()
                sh2 = self.px(self.SHADOW) * 2
                self._paint_grow_mask(p["w"] + sh2, p["h"] + sh2)
            else:
                self.canvas.delete("growmask")
        except Exception:
            import traceback
            log("render 异常：\n" + traceback.format_exc())

    # ── 生命周期 ─────────────────────────────────────────────────
    def _start_watch(self):
        """进程 + 窗口探测约 15ms，丢到后台线程，UI 线程只读最新状态。"""
        self._stop = threading.Event()
        self._state = "open" if self.args.always else detect_state((os.getpid(),))
        if self.args.always:
            return

        def loop():
            while not self._stop.is_set():
                try:
                    self._state = detect_state((os.getpid(),))
                except Exception:
                    self._state = "gone"
                # 隐藏阈值是 1.5 秒，探测周期就不能是 5 秒 —— 否则阈值被粒度吃掉，
                # 关窗后要等四五秒才消失。只有 resident（消失后长期待命不过期）
                # 才有必要在 gone 状态放慢省 CPU。
                fast = not (self.args.resident and self._state == "gone")
                self._stop.wait(self.args.poll if fast else self.args.poll_idle)

        threading.Thread(target=loop, daemon=True).start()

    def _poll(self):
        """生命周期心跳：任何异常都落盘后继续排下一次 —— 心跳链路绝不能断，
        否则小窗就「哑」了（不显示也不退出）。"""
        try:
            self._poll_once()
        except Exception:
            import traceback
            log("poll 异常（链路继续）：\n" + traceback.format_exc())
        if getattr(self, "_quitting", False):
            return
        self.root.after(int(self.args.poll * 1000), self._poll)

    def _poll_once(self):
        now = time.time()
        action = self.life.feed(self._state, now)
        if action == "show":
            self._safe_render()
            try:
                self.root.deiconify()
                self.root.attributes("-topmost", bool(self.args.topmost))
                self.root.lift()  # 别的置顶小工具条（输入法悬浮栏）可能刚把自己提到前面
            except Exception:
                pass
            self.refresh()
            self._auto_collapse_later()
        elif action == "hide":
            try:
                self.root.withdraw()
            except Exception:
                pass
        elif action == "exit":
            self.quit(auto=True)
            return
        if self.life.shown and now - self._last_refresh >= self.args.refresh:
            self.refresh()
        if now - self._theme_ts >= 30:      # 跟随系统深浅色，30 秒看一次
            self._theme_ts = now
            self._check_theme()
        # 下一拍由 _poll 的尾部统一排 —— 这里不再自排，避免双链路

    # ── 交互 ─────────────────────────────────────────────────────
    def _on_press(self, e):
        if self._in_close(e):
            self.quit()
            return
        if self._in_box(getattr(self, "_report_box", None), e):
            self.open_report()
            return
        self._press = (e.x, e.y)
        self._drag = (e.x_root - self.origin[0], e.y_root - self.origin[1])
        self.canvas.configure(cursor="fleur")

    def _on_drag(self, e):
        if not self._drag:
            return
        # 全向拖动：横向决定贴在输入框哪一侧，纵向决定落在哪个高度。
        # 底边不再锁死在输入框底线上 —— 默认位置仍在那一线，但允许手动挪开。
        self._place(e.x_root - self._drag[0], e.y_root - self._drag[1])

    def _on_release(self, e):
        press, self._press = self._press, None
        self.canvas.configure(cursor="")
        moved = abs(e.x - press[0]) + abs(e.y - press[1]) if press else 99
        if self._collapsed:
            self._drag = None
            if moved <= 4:                      # 轻点胶囊 → 展开看详情
                self._set_collapsed(False, animate=True)
                self._auto_collapse_later(4500)
            else:
                # 拖着胶囊走也要能保存：收起态画不下 toast，只置脏，
                # 右键菜单里的「保存当前布局」会带 ● 提示
                self._mark_layout_dirty(silent=True)
            return
        # 几乎没移动 + 点在某一行 → 当成「看详情」，和拖拽互不打扰
        if press and self._drag and moved <= 4 and self._row_at(e.y) >= 0:
            self._drag = None
            self.open_report()
            return
        if self._drag:
            self._drag = None
            self._anchor = (self.origin[0] + self.win_w,
                            self.origin[1] + self.win_h)
            # 拖动只改本次会话的位置，不落盘 —— 落盘要等用户点「保存布局」。
            # 这样误拖一下不会把「指定位置」改坏，重启回到上次保存的地方。
            self._mark_layout_dirty()

    def _mark_layout_dirty(self, silent=False):
        """记录「位置被临时改过」，并给一次轻提示（不写 tray.json）。"""
        self._layout_dirty = True
        self._plan_cache = None
        if silent:
            return
        self._toast = ("位置已调整 · 右键可保存布局", time.time() + 2.2)
        self._safe_render()
        self.root.after(2300, self._clear_toast)

    def tray_corner(self):
        """当前小窗（含胶囊态）距工作区右下角的偏移 —— 锚点即胶囊位置。"""
        wa = self.wa()
        x, y = self.origin
        dx, dy = corner_offset(x, y, self.win_w, self.win_h, wa)
        return {"dx": int(dx), "dy": int(dy)}

    def save_layout_now(self):
        """把当前布局固化为启动位置（小窗 + 开着的大窗一起存）。"""
        patch = {"tray": self.tray_corner()}
        win = getattr(self, "_report_win", None)
        if win is not None:
            try:
                patch["report"] = win.corner()
            except Exception:
                pass
        save_layout(patch)
        self._layout_dirty = False
        if win is not None:
            try:
                win.mark_saved()
            except Exception:
                pass
        self._toast = ("布局已保存", time.time() + 1.6)
        self._safe_render()
        self.root.after(1700, self._clear_toast)

    def reset_layout(self):
        """恢复内置默认位置（右下角贴输入框底线），并清掉存档。"""
        clear_layout()
        self._layout_dirty = False
        self._plan_cache = None
        wa = self.wa()
        x, y = default_pos(self.win_w, self.win_h, wa, margin=self.px(8),
                           bottom_gap=self.px(self.BOTTOM_PIN))
        self.origin = (x, y)
        self._anchor = (x + self.win_w, y + self.win_h)
        self._apply_state_pos(animate=True)
        self._safe_render()
        win = getattr(self, "_report_win", None)
        if win is not None:
            try:
                win.reset_pos()
            except Exception:
                pass
        self._toast = ("已恢复默认位置", time.time() + 1.6)
        self.root.after(1700, self._clear_toast)

    def _in_close(self, e):
        box = getattr(self, "_close_box", None)
        if not box:
            return False
        return box[0] <= e.x <= box[2] and box[1] <= e.y <= box[3]

    def _on_enter(self, _e):
        """碰到胶囊就准备展开：停 180ms 才动手，快速划过不会闪。"""
        self._cancel_collapse()
        if self._collapsed and self._hover_job is None:
            self._pill_hover = True
            self._safe_render()
            self._hover_job = self.root.after(180, self._expand_now)

    def _expand_now(self):
        self._hover_job = None
        if self._collapsed and self._pointer_inside():
            self._set_collapsed(False, animate=True)
            self._cancel_collapse()

    def _cancel_hover(self):
        if self._hover_job:
            try:
                self.root.after_cancel(self._hover_job)
            except Exception:
                pass
            self._hover_job = None

    def _on_leave(self, _e):
        """鼠标移开 2.2 秒后缩回胶囊；拖动中或锁定时不缩。"""
        self._cancel_hover()
        if self._pill_hover:
            self._pill_hover = False
            if self._collapsed:
                self._safe_render()
        if self._pinned or self._drag:
            return
        self._cancel_collapse()
        self._collapse_job = self.root.after(2200, self._do_collapse)

    def _cancel_collapse(self):
        if self._collapse_job:
            try:
                self.root.after_cancel(self._collapse_job)
            except Exception:
                pass
            self._collapse_job = None

    def _do_collapse(self):
        self._collapse_job = None
        if self._pinned:
            return
        if self._pointer_inside():      # 鼠标还在看，就再等等
            self._collapse_job = self.root.after(1800, self._do_collapse)
            return
        self._set_collapsed(True, animate=True)

    def _pointer_inside(self):
        try:
            mx, my = self.root.winfo_pointerx(), self.root.winfo_pointery()
        except Exception:
            return False
        # 窗口可能比视觉尺寸大（恒为展开尺寸），按当前 plan 的视觉矩形判定
        p = self._plan()
        sh2 = self.px(self.SHADOW) * 2
        w, h = p["w"] + sh2, p["h"] + sh2
        x0 = self.origin[0] + self.win_w - w
        y0 = self.origin[1] + self.win_h - h
        return (x0 <= mx <= x0 + w and y0 <= my <= y0 + h)

    def _auto_collapse_later(self, delay=2600):
        """刚出现时不会触发 Leave，所以启动后主动看一眼鼠标在不在窗口上。"""
        if self._pinned:
            return

        def check():
            if not self._pointer_inside():
                self._do_collapse()

        self.root.after(delay, check)

    def _row_at(self, y):
        """画布 y → 第几行模型（-1 表示不在行区）。"""
        if self.report.get("error"):
            return -1
        rows, _ = pick_rows(self.report, self.args.max_rows)
        top = self.px(self.SHADOW) + self.px(self.HEAD_H)
        if y < top:
            return -1
        idx = int((y - top) // self.px(self.ROW_H))
        return idx if 0 <= idx < len(rows) else -1

    def _on_motion(self, e):
        hover_close = self._in_close(e)
        hover_report = self._in_box(getattr(self, "_report_box", None), e)
        hover_row = self._row_at(e.y)
        if (hover_close, hover_report, hover_row) != \
                (self._hover_close, self._hover_report, self._hover_row):
            self._hover_close = hover_close
            self._hover_report = hover_report
            self._hover_row = hover_row
            hot = hover_close or hover_report or hover_row >= 0
            self.canvas.configure(cursor="hand2" if hot else "")
            self._safe_render()

    def _in_box(self, box, e):
        return bool(box) and box[0] <= e.x <= box[2] and box[1] <= e.y <= box[3]

    def _check_theme(self):
        dark = system_is_dark()
        if dark != self._dark:
            self._dark = dark
            self.pal = PALETTE["dark" if dark else "light"]
            self._safe_render()

    def _on_menu(self, e):
        """右键菜单。

        复用同一个 Menu 对象，**绝不能在 tk_popup 之后 destroy**：`tk_popup` 不阻塞，
        它 post 完就返回，紧跟其后的 destroy 会把菜单当场收掉 —— 用户看到的是
        「右键有一下、但点不动任何选项」。复用还顺手解决了每次右键新建 widget 的泄漏。
        """
        tk = self.tk
        m = getattr(self, "_menu", None)
        if m is None or not self._menu_alive(m):
            m = tk.Menu(self.root, tearoff=0, font=(self.family, -self.px(12)),
                        bg=self.pal["card"], fg=self.pal["text"],
                        activebackground=mix(self.pal["card"], self.pal["text"], 0.10),
                        activeforeground=self.pal["text"], bd=0, relief="flat")
            self._menu = m
        m.delete(0, "end")
        self.var_top.set(bool(self.args.topmost))
        self.var_away.set(not self._show_when_away)
        self.var_avoid.set(self._avoid_input)
        self.var_pin.set(self._pinned)
        self.var_boot.set(startup_installed())

        m.add_command(label="刷新", command=self.refresh)
        m.add_command(label="完整报告", command=self.open_report)
        m.add_command(label="在控制台打开", command=self.open_report_console)
        m.add_command(label="复制摘要", command=self.copy_summary)
        m.add_separator()
        m.add_command(label="保存当前布局" + (" ●" if self._layout_dirty else ""),
                      command=self.save_layout_now)
        m.add_command(label="恢复默认位置", command=self.reset_layout)
        m.add_command(label="重新校准位置", command=self.recalibrate)
        m.add_checkbutton(label="切到其他应用时隐藏", variable=self.var_away,
                          command=self.toggle_away)
        m.add_checkbutton(label="展开时不压输入框", variable=self.var_avoid,
                          command=self.toggle_avoid)
        m.add_checkbutton(label="保持展开", variable=self.var_pin, command=self.toggle_pin)
        m.add_checkbutton(label="窗口置顶", variable=self.var_top, command=self.toggle_top)
        m.add_checkbutton(label="开机自动待命", variable=self.var_boot, command=self.toggle_boot)
        m.add_separator()
        m.add_command(label="重启小窗", command=self.restart)
        m.add_command(label="退出", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        except Exception:
            log("菜单弹出失败：\n" + __import__("traceback").format_exc())
        finally:
            try:
                m.grab_release()      # 清掉可能的残留全局 grab，别让菜单吃掉后续鼠标
            except Exception:
                pass

    @staticmethod
    def _menu_alive(m):
        try:
            return bool(m.winfo_exists())
        except Exception:
            return False

    def recalibrate(self):
        """重新读屏量一次输入框边界（换了窗口大小/缩放后手动点一下）。

        量之前必须先把卡片收成胶囊：展开的卡片正压在扫描线上，`_probe_edge`
        会把**自己的左缘**当成输入框右缘 —— 量到的值偏小，卡片随后就往左长宽，
        直接盖住输入框（用户截图就是这个）。收起后窗口只剩右下角一枚胶囊，
        扫描线经过的区域露出的是宿主窗口本体。
        """
        was_expanded = not self._collapsed
        if was_expanded:
            self._set_collapsed(True)
            try:
                self.root.update_idletasks()
                self.root.update()
            except Exception:
                pass
        self._edge_cache = 0
        self._edge_ts = 0.0
        edge = self._probe_edge()
        if edge and self._anchor:
            # 自检：按这个边界算出来的卡片左缘，必须落在边界右侧。
            # 落在左侧说明读到的是自己（或别的窗口），宁可不用也不要把卡片放宽压上去。
            self._edge_cache = edge
            self._plan_cache = None
            try:
                probe_plan = self._plan(as_expanded=True)
                if self._anchor[0] - probe_plan["w"] < edge - self.px(2):
                    edge = None
            except Exception:
                pass
        if edge:
            self._edge_cache = edge
            try:
                st = load_state(); st["input_edge"] = edge; save_state(st)
            except Exception:
                pass
            self._toast = (f"校准 {edge}px", time.time() + 1.6)
        else:
            self._toast = ("读不到输入框", time.time() + 1.6)
        self._plan_cache = None
        if was_expanded:
            self._set_collapsed(False, animate=True)
        else:
            self._apply_state_pos(animate=True)
            self._safe_render()
        self.root.after(1700, self._clear_toast)

    def toggle_away(self):
        """切到别的应用时是否让路（默认让）。关掉就一直置顶显示。"""
        hide = bool(self.var_away.get())
        self._show_when_away = not hide
        st = load_state()
        st["always_on_top_show"] = self._show_when_away
        save_state(st)
        try:
            self.life.hide_on_away = hide
            if not hide:
                self.life.away_since = None
        except Exception:
            pass

    def toggle_avoid(self):
        self._avoid_input = bool(self.var_avoid.get())
        st = load_state()
        st["avoid_input"] = self._avoid_input
        save_state(st)
        if not self._collapsed:
            self._apply_state_pos(animate=True)
        self._safe_render()

    def toggle_pin(self):
        self._pinned = bool(self.var_pin.get())
        st = load_state()
        st["pinned"] = self._pinned
        save_state(st)
        if self._pinned:
            self._cancel_collapse()
            self._set_collapsed(False)

    def toggle_top(self):
        self.args.topmost = bool(self.var_top.get())
        try:
            self.root.attributes("-topmost", self.args.topmost)
        except Exception:
            pass
        st = load_state()
        st["topmost"] = self.args.topmost
        save_state(st)

    def toggle_boot(self):
        ok, msg = (uninstall_startup() if not self.var_boot.get() else install_startup())
        log(msg)
        self.var_boot.set(startup_installed())

    def copy_summary(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(summary_lines(self.report)))
        except Exception:
            return
        self._toast = ("已复制", time.time() + 1.2)
        self._safe_render()
        self.root.after(1300, self._clear_toast)

    def _clear_toast(self):
        if getattr(self, "_quitting", False):   # 已 destroy 的 root 不能再排渲染
            return
        self._toast = None
        self._safe_render()

    def open_report(self):
        """打开完整报告窗口（GUI）。已开着就提到前面，不重复开。"""
        win = getattr(self, "_report_win", None)
        if win is not None:
            try:
                win.top.deiconify()
                win.top.lift()
                win.refresh()
                return
            except Exception:
                self._report_win = None
        self._report_win = ReportWindow(self)
        if not self._pinned:            # 报告开了就把小窗收成胶囊，别两个窗口一起占屏
            self._set_collapsed(True)

    def open_report_console(self):
        """在控制台窗口里跑完整报告。

        连点会一次弹好几个控制台，所以 1.5 秒内只认第一次。
        

        不能直接 `python quota_watch.py`：脚本打印完表格就 exit，控制台窗口立刻销毁，
        用户只看到闪一下、以为「点了没反应」。所以走 run.bat（末尾有 pause 留住窗口），
        实在没有 run.bat 就用 `cmd /k` 包一层。
        """
        flags = 0x00000010 if os.name == "nt" else 0  # CREATE_NEW_CONSOLE
        script = os.path.join(HERE, "quota_watch.py")
        bat = os.path.join(HERE, "run.bat")
        try:
            if os.name == "nt" and os.path.exists(bat):
                subprocess.Popen(["cmd", "/c", bat], cwd=HERE, creationflags=flags)
            elif os.name == "nt":
                exe = sys.executable or "python"
                if os.path.basename(exe).lower().startswith("pythonw"):
                    alt = os.path.join(os.path.dirname(exe), "python.exe")
                    if os.path.exists(alt):
                        exe = alt
                subprocess.Popen(["cmd", "/k", exe, script], cwd=HERE, creationflags=flags)
            else:
                subprocess.Popen([sys.executable or "python", script], cwd=HERE)
        except Exception as exc:
            log(f"打开报告失败：{exc}")
            self._fail_note("打不开完整报告", f"{exc}\n\n可以直接双击 run.bat，或用命令行跑："
                                          f"\npython {script}")

    def _fail_note(self, title, msg):
        """静默失败最坑人：出错时至少弹一个框说明原因。"""
        try:
            from tkinter import messagebox
            messagebox.showwarning(title, msg, parent=self.root)
        except Exception:
            pass

    def restart(self):
        """热重启：延迟拉起新实例后自己退出。

        改完代码不用去任务管理器杀进程、也不用手动等看门狗的 15 分钟静默期。
        延迟是必须的 —— 新实例要和本进程的互斥体错开。
        """
        spawn_after(2.0)
        # auto=True 不写 suppress：万一延迟启动器没起来，看门狗 5 秒内还能兜底补上
        self.quit(auto=True)

    def quit(self, auto=False):
        """auto=True 表示「WorkBuddy 退出了，我自己收工」，而不是用户手动关闭。

        用户手动关掉时要留一个抑制标记：看门狗看到它，15 分钟内不会又把小窗
        拉起来 —— 否则点了 × 五秒后它自己回来，非常烦人。
        """
        if not auto:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(SUPPRESS_FILE, "w", encoding="utf-8") as fh:
                    fh.write(str(int(time.time())))
            except OSError:
                pass
        self._quitting = True              # _poll 看到就不再往已销毁的 root 排程
        try:
            if self._menu is not None:     # 菜单还持着 grab 时 destroy root 会报错
                self._menu.unpost()
        except Exception:
            pass
        self.life.on_user_quit()
        try:
            self._stop.set()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


class ReportWindow:
    """完整报告窗口：把小窗里的一行展开成每个模型的完整档案。

    控制台版本（run.bat）仍然保留，但默认入口给 GUI —— 不弹黑框，也不需要按键关闭。
    """

    W_LOGICAL = 870   # 宽度 ≈ 输入框横向跨度（本机实测 832~2148 物理px）
    TOP_MARGIN = 48   # 距工作区顶部（逻辑 px）：给 WorkBuddy 自己的标题行留一排
    # 底边与输入框上沿的间距：除了留白，还要让开输入框上方右侧的
    # 吉祥物（猫，本机实测 x 1990~2134 / y 1051~1187），底边必须在其上方
    INPUT_GAP = 162
    INPUT_TOP_FROM_WA_BOTTOM = 167  # 输入框上沿距工作区底部（本机实测 250 物理 / 1.5）
    PAD = 22
    HEAD = 56
    META = 38
    CARD = 176       # 每模型一张卡：卡间距 20，白卡浮在灰底上，一眼一张
    FOOT = 52
    COL_W = 700      # 内容列最大宽度（逻辑 px）：宽屏上进度条不至于横跨全窗
    SCROLL_STEP = 46  # 滚轮一格的内容位移（逻辑 px），约三行文字
    SHADOW = 6       # 整窗三层投影：把报告窗从宿主应用上「托」起来

    def __init__(self, app):
        tk = app.tk
        self.app = app
        self.s = app.s
        self.pal = app.pal
        self.top = tk.Toplevel(app.root)
        self.top.withdraw()
        self.top.title("配额胶囊 · 完整报告")
        try:
            self.top.overrideredirect(True)
            self.top.attributes("-transparentcolor", C_KEY)
        except Exception:
            pass

        fam = app.family
        mono = app.mono
        px = self.px
        self.f = {
            "title": tkfont_font(tk, fam, -px(15), "bold"),
            "name": tkfont_font(tk, fam, -px(14), "bold"),
            "pct": tkfont_font(tk, mono, -px(20), "bold"),
            "sub": tkfont_font(tk, fam, -px(12)),
            "meta": tkfont_font(tk, fam, -px(10.5)),
            "small": tkfont_font(tk, fam, -px(10)),
        }
        # 双画布：标题栏固定不滚，内容画布独立滚动 —— Tk 画布自带边界裁剪，
        # 内容滚到多高都不会漫进标题栏（旧版单画布，卡片会叠到标题上）
        self.canvas = tk.Canvas(self.top, bg=C_KEY, highlightthickness=0, bd=0,
                                height=px(self.HEAD) + px(self.SHADOW))
        self.canvas.pack(side="top", fill="x")
        self.body = tk.Canvas(self.top, bg=C_KEY, highlightthickness=0, bd=0)
        self.body.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_head_motion)
        self.canvas.bind("<Leave>", self._on_head_leave)
        self.canvas.bind("<Button-3>", self._on_menu)
        self.body.bind("<ButtonPress-1>", self._on_body_press)
        self.body.bind("<B1-Motion>", self._on_drag)
        self.body.bind("<ButtonRelease-1>", self._on_release)
        self.body.bind("<Button-3>", self._on_menu)
        self.body.bind("<MouseWheel>", self._on_wheel)
        self.top.bind("<MouseWheel>", self._on_wheel)
        self.top.bind("<Escape>", lambda e: self.close())

        self.report = app.report
        self._drag = False
        self._origin = (0, 0)
        self._press_root = None
        self._hover_close = False
        self._hover_reload = False
        self._pos_dirty = False    # 拖过但还没保存 → 标题栏浮出「保存位置」
        self._save_box = None
        self._menu = None
        self._busy = False
        self._scanned = None
        self._scroll = 0          # 内容滚动偏移，滚轮更新，_render 里夹取
        self._max_scroll = 0
        self._resize()
        if not self._load_report_pos():
            self._place_center()   # 用户拖过就 restore，没拖过才用默认落点
        self._render()
        self.top.deiconify()
        try:
            self.top.lift()
            self.top.focus_force()
        except Exception:
            pass

    # ── 尺寸与位置 ───────────────────────────────────────────────
    def px(self, v):
        return max(1, int(round(v * self.s)))

    def _resize(self):
        """尺寸按用户指定的摆放区域定（2026-11-06 截图圈定）：

        横向 = 输入框的跨度（W_LOGICAL ≈ 832~2148 的宽度），
        纵向 = 从工作区顶部往下，底边停在输入框上沿之上 —— 即压在对话内容区上。
        """
        sw = self.top.winfo_screenwidth()
        wa = tk_work_area(sw)
        self.cw = min(self.px(self.W_LOGICAL), (wa[2] - wa[0]) - self.px(16))
        top = wa[1] + self.px(self.TOP_MARGIN)
        bottom = wa[3] - self.px(self.INPUT_TOP_FROM_WA_BOTTOM) - self.px(self.INPUT_GAP)
        self.ch = max(self.px(420), bottom - top)
        self._top_y = top
        self.top.geometry(f"{self.cw}x{self.ch}")

    def _place_center(self):
        """右缘与输入框右缘平齐（input_edge 是现成量好的），量不到就水平居中。"""
        wa = tk_work_area(self.top.winfo_screenwidth())
        edge = 0
        try:
            edge = int(self.app._input_edge() or 0)
        except Exception:
            edge = 0
        if edge:
            x = edge - self.px(4) - self.cw
        else:
            x = wa[0] + (wa[2] - wa[0] - self.cw) // 2
        x = max(wa[0] + self.px(8), min(x, wa[2] - self.cw - self.px(8)))
        y = getattr(self, "_top_y", wa[1] + self.px(self.TOP_MARGIN))
        self._origin = (x, y)
        self.top.geometry(f"{self.cw}x{self.ch}+{x}+{y}")

    # ── 绘制 ─────────────────────────────────────────────────────
    def _render(self):
        self._render_head()
        self._render_body()

    def _render_head(self):
        """标题栏：固定画布，不随内容滚动（关闭按钮必须一直在）。

        与卡片同色的一条标题带 + 底部分隔线 + 整窗投影 —— 不再用墨色制造
        突兀的对比，整窗融进宿主应用的白色系，读起来是「应用自己的一块面板」。
        """
        c, p, px = self.canvas, self.pal, self.px
        c.delete("all")
        pad = px(self.PAD)
        w = self.cw
        sh = px(self.SHADOW)
        rad = px(14)

        # 边框：只有一圈 1px 极细描边（win_edge），无投影。
        head_h = px(self.HEAD)
        c.create_polygon(round_rect_points(sh, sh, w - sh, sh + head_h + px(24), rad),
                         fill=p["head_bg"], outline=p["win_edge"])

        cy = sh + head_h // 2
        c.create_line(sh + px(1), sh + head_h - px(1),
                      w - sh - px(1), sh + head_h - px(1), fill=p["edge_soft"])
        c.create_text(pad + sh, cy, anchor="w", text="配额胶囊 · 完整报告",
                      font=self.f["title"], fill=p["head_fg"])

        # 位置被拖动过：标题右侧浮出一枚「保存为默认位置」chip —— 位置只在这里固化，
        # 不点它下次打开还回旧位置。这样误拖一下不会改坏已保存的布局。
        if self._pos_dirty:
            tw = self.f["title"].measure("配额胶囊 · 完整报告")
            bx0 = pad + sh + tw + px(14)
            bx1 = bx0 + px(118)
            by0, by1 = cy - px(11), cy + px(11)
            c.create_polygon(round_rect_points(bx0, by0, bx1, by1, px(10)),
                             fill=mix(p["card"], p["text"], 0.10),
                             outline=mix(p["card"], p["text"], 0.28))
            c.create_text((bx0 + bx1) / 2, cy, text="保存为默认位置",
                          font=self.f["small"], fill=p["text"])
            self._save_box = (bx0, by0, bx1, by1)
        else:
            self._save_box = None

        # 右上：刷新 / 关闭。悬停时点亮一枚圆底
        self._close_box = (w - pad - sh - px(26), sh + px(10),
                           w - pad - sh + px(6), sh + px(40))
        self._reload_box = (w - pad - sh - px(64), sh + px(10),
                            w - pad - sh - px(30), sh + px(40))
        for box, glyph, hov in ((self._reload_box, "⟳", self._hover_reload),
                                (self._close_box, "×", self._hover_close)):
            if hov:
                c.create_oval(box[0] + px(2), box[1] + px(1),
                              box[2] - px(2), box[3] - px(1),
                              fill=p["hover"], outline="")
            c.create_text((box[0] + box[2]) / 2, cy, text=glyph,
                          font=self.f["sub"],
                          fill=p["text"] if hov else p["sub"])

    def _on_head_motion(self, e):
        """标题栏按钮悬停：改光标 + 重绘标题画布（内容画布不动）。

        空白处给四向箭头 —— 整条标题带都能拖窗口，得让人一眼看出来。
        """
        hc = self._in(self._close_box, e)
        hr = self._in(self._reload_box, e)
        hs = self._in(getattr(self, "_save_box", None), e)
        if (hc, hr) != (self._hover_close, self._hover_reload):
            self._hover_close, self._hover_reload = hc, hr
            self._render_head()
        cur = "hand2" if (hc or hr or hs) else "fleur"
        if cur != getattr(self, "_cursor", None):
            self._cursor = cur
            try:
                self.canvas.configure(cursor=cur)
            except Exception:
                pass

    def _on_head_leave(self, _e):
        if self._hover_close or self._hover_reload:
            self._hover_close = self._hover_reload = False
            self._render_head()
        if getattr(self, "_cursor", None):
            self._cursor = None
            try:
                self.canvas.configure(cursor="")
            except Exception:
                pass

    def _render_body(self):
        """内容画布：概览行 → 卡片 → 图例 → 建议，滚轮平移，画布边界裁剪。"""
        c, p, px = self.body, self.pal, self.px
        c.delete("all")
        w = self.cw
        sh = px(self.SHADOW)
        rad = px(14)
        bh = max(1, self.ch - px(self.HEAD) - sh)

        # 窗底色：比卡片明显暗一档的灰底，白卡放上去才有「卡片在画布上」的层次
        if self.app._dark:
            bg = mix(p["card"], "#000000", 0.24)
        else:
            bg = mix(p["card"], p["sub"], 0.11)
        c.create_polygon(round_rect_points(sh, -px(30), w - sh, bh, rad),
                         fill=bg, outline=p["win_edge"])

        # 内容列：限宽居中（在卡片内缩范围内）。宽屏上全宽卡片会把进度条拉到 1700px
        pad = px(self.PAD)
        inner_x0, inner_x1 = sh + pad, w - sh - pad
        col_w = min(inner_x1 - inner_x0, px(self.COL_W))
        cx0 = inner_x0 + ((inner_x1 - inner_x0) - col_w) // 2
        cx1 = cx0 + col_w

        rows = live_models(self.report)
        cards_h = px(self.CARD) * max(1, len(rows))
        has_legend = bool(rows) and not self.report.get("error")
        legend_h = px(44) if has_legend else 0
        content_h = px(self.META) + cards_h + legend_h + px(self.FOOT)
        avail = bh - px(12)
        self._max_scroll = max(0, content_h - avail)
        self._scroll = clamp_scroll(getattr(self, "_scroll", 0), self._max_scroll)
        y0 = (max(0, (avail - content_h) // 2) if self._max_scroll <= 0
              else -self._scroll)

        # 概览行，与内容列对齐
        meta_y = y0 + px(self.META) // 2
        gen = self.report.get("generated_at") or "—"
        info = (f"生成 {gen} · 增速窗口 {self.report.get('window_min', 60)} 分钟 · "
                f"统计口径 {self.report.get('cycle_hours', 24):g}h 周期 · "
                f"{self.report.get('files', 0)} 个会话文件 / {self.report.get('records', 0)} 条快照")
        c.create_text(cx0, meta_y, anchor="w", text=info, font=self.f["small"], fill=p["sub"])

        y = y0 + px(self.META)
        if self.report.get("error"):
            t1, t2 = error_texts(self.report["error"])
            midx = (cx0 + cx1) / 2
            midy = y + (bh - y) / 2 - px(24)
            rg = px(30)
            c.create_arc(midx - rg, midy - rg, midx + rg, midy + rg,
                         start=25, extent=220, style="arc",
                         outline=p["track"], width=px(4))
            c.create_line(midx, midy, midx + rg * 0.60, midy - rg * 0.60,
                          fill=p["sub"], width=px(2.5))
            c.create_oval(midx - px(3), midy - px(3), midx + px(3), midy + px(3),
                          fill=p["sub"], outline="")
            c.create_text(midx, midy + rg + px(26), text=t1,
                          font=self.f["name"], fill=p["text"])
            c.create_text(midx, midy + rg + px(50), text=t2,
                          font=self.f["sub"], fill=p["sub"])
        else:
            for i, r in enumerate(rows):
                self._draw_card(c, r, cx0, cx1, y, i, len(rows))
                y += px(self.CARD)

        # 图例 / 口径：跟在卡片后面一起滚 —— 绝不再叠到卡片上
        # （注意 y 在上面的循环里已经走完 cards_h，别再重复累加）
        if has_legend:
            self._draw_legend(c, cx0, cx1, y + px(12))
            fy = y + px(12) + legend_h
        else:
            fy = y
        c.create_line(cx0, fy, cx1, fy, fill=p["edge_soft"])
        # 建议放一枚语义色淡底的提示框里：有风险时红调浮出，没有时安静灰底
        risk = bool(self.report.get("has_risk"))
        if risk:
            msg = "有模型会在配额重置前耗尽：先把长任务的关键结论落盘，再切模型或拆小步。"
            tint = mix(p["level"]["critical"], p["card"], 0.94)
            edge_col = mix(p["level"]["critical"], p["card"], 0.75)
            msg_col = p["level"]["critical"]
        else:
            msg = "按当前节奏，没有模型会在配额重置前耗尽。"
            tint = p["card"]          # 灰底上用白框，安静但不糊底
            edge_col = p["edge_soft"]
            msg_col = p["text"]
        by0, by1 = fy + px(7), fy + px(39)
        c.create_polygon(round_rect_points(cx0, by0, cx1, by1, px(8)),
                         fill=tint, outline=edge_col)
        c.create_text(cx0 + px(14), (by0 + by1) / 2, anchor="w", text=msg,
                      font=self.f["sub"], fill=msg_col)

        self._scrollbar(c, bh)

    def _scrollbar(self, c, bh):
        """可滚时在右缘画一根细指示条，提示下面还有内容。"""
        if self._max_scroll <= 0:
            return
        p, px = self.pal, self.px
        x = self.cw - px(self.SHADOW) - px(10)
        top, avail = px(6), bh - px(12)
        c.create_polygon(round_rect_points(x, top, x + px(4), top + avail, px(2)),
                         fill=p["edge_soft"], outline="")
        thumb_h = max(px(28), int(avail * avail / float(self._max_scroll + avail)))
        top_y = top + int((avail - thumb_h) * (self._scroll / float(self._max_scroll)))
        c.create_polygon(round_rect_points(x, top_y, x + px(4), top_y + thumb_h, px(2)),
                         fill=mix(p["sub"], p["card"], 0.35), outline="")

    def _draw_legend(self, c, xl, xr, top):
        """图例只留一行四档色标 —— 口径说明文字删掉，读一次就懂的事不值得常驻。"""
        p, px = self.pal, self.px
        c.create_line(xl, top, xr, top, fill=p["edge_soft"])
        y = top + px(24)
        x = xl
        for lv in ("critical", "warn", "ok", "idle"):
            c.create_oval(x, y - px(3), x + px(6), y + px(3),
                          fill=p["level"][lv], outline="")
            c.create_text(x + px(11), y, anchor="w", text=LEVEL_CN[lv],
                          font=self.f["small"], fill=p["sub"])
            x += px(11) + self.f["small"].measure(LEVEL_CN[lv]) + px(22)

    def _meta_line(self, c, x, y, segs):
        """一行「标签 + 值」段：标签压淡、值略深，段间以「·」分隔。

        比把六项挤成一串点分文本好扫读得多 —— 标签退后，数字站出来。
        """
        p, px, f = self.pal, self.px, self.f["meta"]
        lab_col = mix(p["sub"], p["card"], 0.40)
        # 值用接近正文的深色：标签退后、数字站出来，两行档案才扫得动
        val_col = mix(p["text"], p["sub"], 0.15)
        dot_col = mix(p["sub"], p["card"], 0.55)
        for i, (lab, val) in enumerate(segs):
            if i:
                c.create_text(x, y, anchor="w", text="·", font=f, fill=dot_col)
                x += f.measure("·") + px(6)
            c.create_text(x, y, anchor="w", text=lab, font=f, fill=lab_col)
            x += f.measure(lab) + px(4)
            c.create_text(x, y, anchor="w", text=val, font=f, fill=val_col)
            x += f.measure(val) + px(12)

    def _draw_card(self, c, r, xl, xr, top, idx, total):
        p, px = self.pal, self.px
        accent = p["level"].get(r.get("level"), p["sub"])
        h = px(self.CARD)
        # 卡片底：白卡 + 细边 + 单层右下落影（浮在灰底上，靠间距和色差分区）
        c.create_polygon(round_rect_points(xl + px(3), top + px(13), xr + px(2),
                                           top + h - px(8), px(10)),
                         fill=p["shadow"][2], outline="")
        c.create_polygon(round_rect_points(xl, top + px(8), xr, top + h - px(12), px(10)),
                         fill=p["card"], outline=p["edge"])
        ix = xl + px(22)
        ir = xr - px(16)
        # 左侧档位色条：圆头短竖条，与名称块同高，扫读时先看到颜色再看文字
        bw_ = px(3)
        c.create_polygon(round_rect_points(ix - px(12), top + px(24),
                                           ix - px(12) + bw_, top + px(46), bw_ / 2.0),
                         fill=accent, outline="")

        name, pct, sub = row_texts(r)
        pct_w = self.f["pct"].measure(pct)
        c.create_text(ix, top + px(34), anchor="w",
                      text=self.app._elide(self.f["name"], name, (ir - ix) - pct_w - px(12)),
                      font=self.f["name"], fill=p["text"])
        c.create_text(ir, top + px(34), anchor="e", text=pct, font=self.f["pct"], fill=accent)

        by = top + px(54)
        bh = px(6)
        c.create_polygon(round_rect_points(ix, by, ir, by + bh, bh / 2.0),
                         fill=p["track"], outline="")
        fw = bar_fill(r.get("percent_used", 0), ir - ix)
        if fw > 0:
            c.create_polygon(round_rect_points(ix, by, ix + fw, by + bh, bh / 2.0),
                             fill=accent, outline="")
            c.create_polygon(
                round_rect_points(ix + px(1), by + px(1),
                                  max(ix + px(2), ix + fw - px(1)), by + px(3), px(1.4)),
                fill=mix(accent, "#ffffff", 0.26), outline="")

        c.create_text(ix, top + px(80), anchor="w",
                      text=self.app._elide(self.f["sub"], sub, ir - ix),
                      font=self.f["sub"], fill=p["sub"])

        c.create_line(ix, top + px(98), ir, top + px(98), fill=p["edge_soft"])

        rate = r.get("rate_pct_per_hour")
        eta = r.get("eta_hours_to_limit")
        line1 = [
            ("重置", str(r.get("reset_local") or "—")),
            ("增速", f"{rate:g}%/h" if rate else "—"),
            ("距重置", qw.fmt_dur(r.get("hours_to_reset"))),
        ]
        line2 = [
            ("触限预测", f"约 {qw.fmt_dur(eta)}后" if eta is not None else "暂不预测"),
            ("样本", f"{r.get('requests_cycle', r.get('requests', 0))} 次"),
            ("置信", str(qw.CONF_CN.get(r.get("rate_confidence"), "—"))),
        ]
        self._meta_line(c, ix, top + px(116), line1)
        self._meta_line(c, ix, top + px(134), line2)

        tk_txt = (f"本周期 {r.get('tokens_cycle', 0):,} tokens"
                  f" · {r.get('credit_cycle', 0):g} 积分")
        if r.get("shared_models"):
            tk_txt += f" · 与 {', '.join(r['shared_models'])} 共用配额"
        c.create_text(ix, top + px(154), anchor="w", text=tk_txt,
                      font=self.f["small"], fill=mix(p["sub"], p["card"], 0.35))

    # ── 交互 ─────────────────────────────────────────────────────
    def _on_press(self, e):
        if self._in(self._close_box, e):
            self.close()
            return
        if self._in(self._reload_box, e):
            self.refresh()
            return
        if self._in(getattr(self, "_save_box", None), e):
            self.save_pos()
            return
        # 标题栏整条都能按（canvas 本身就只占标题带那一块）
        self._start_drag(e)

    def _on_body_press(self, e):
        """内容区按下也能拖窗口 —— 画布上没有可点选的元素，不会抢滚动/点击。"""
        self._start_drag(e)

    def _start_drag(self, e):
        self._drag = True
        self._off = (e.x_root - self._origin[0], e.y_root - self._origin[1])
        self._press_root = (e.x_root, e.y_root)

    def _on_drag(self, e):
        if not self._drag:
            return
        x = e.x_root - self._off[0]
        y = e.y_root - self._off[1]
        self._origin = (x, y)
        self.top.geometry(f"{self.cw}x{self.ch}+{x}+{y}")

    def _on_release(self, e):
        was_dragging = self._drag
        self._drag = False
        pr, self._press_root = getattr(self, "_press_root", None), None
        if not was_dragging:
            return
        moved = 99 if not pr else abs(e.x_root - pr[0]) + abs(e.y_root - pr[1])
        if moved <= 3:
            return                    # 只是点了一下，位置没动就别报「待保存」
        # 拖完只夹进工作区，不落盘 —— 标题栏会浮出「保存为默认位置」chip，
        # 点了才固化为启动位置。误拖一下不会改坏上次保存的布局。
        wa = tk_work_area(self.top.winfo_screenwidth())
        x = max(wa[0], min(int(self._origin[0]), wa[2] - self.cw))
        y = max(wa[1], min(int(self._origin[1]), wa[3] - self.ch))
        self._origin = (x, y)
        self.top.geometry(f"{self.cw}x{self.ch}+{x}+{y}")
        self._pos_dirty = True
        self._render_head()

    def _load_report_pos(self):
        """启动位置：优先 layout.report，其次旧平铺字段；都没有返回 False 走默认落点。"""
        lay = layout_part("report")
        dx, dy = lay.get("dx"), lay.get("dy")
        if not (isinstance(dx, int) and isinstance(dy, int)):
            return False
        wa = tk_work_area(self.top.winfo_screenwidth())
        x = wa[2] - self.cw - max(0, dx)
        y = wa[3] - self.ch - max(0, dy)
        x = max(wa[0], min(x, wa[2] - self.cw))
        y = max(wa[1], min(y, wa[3] - self.ch))
        self._origin = (x, y)
        self.top.geometry(f"{self.cw}x{self.ch}+{x}+{y}")
        return True

    def corner(self):
        """当前大窗距工作区右下角的偏移，与小窗同一套坐标约定。"""
        wa = tk_work_area(self.top.winfo_screenwidth())
        x, y = self._origin
        return {"dx": int(max(0, wa[2] - (x + self.cw))),
                "dy": int(max(0, wa[3] - (y + self.ch)))}

    def save_pos(self):
        """固化当前大窗位置（顺手把小窗当前值一起写，避免两处各存一半）。"""
        patch = {"report": self.corner()}
        try:
            patch["tray"] = self.app.tray_corner()
            self.app._layout_dirty = False      # 小窗的脏标记一起清，否则菜单一直带 ●
        except Exception:
            pass
        save_layout(patch)
        self.mark_saved()

    def mark_saved(self):
        if self._pos_dirty:
            self._pos_dirty = False
            self._render_head()

    def reset_pos(self):
        """回到默认落点：右缘对齐输入框，顶距工作区 TOP_MARGIN。"""
        self._pos_dirty = False
        self._place_center()
        self._render_head()

    def _on_wheel(self, e):
        if self._max_scroll <= 0:
            return
        step = self.px(self.SCROLL_STEP)
        self._scroll = clamp_scroll(self._scroll - (step if e.delta > 0 else -step),
                                    self._max_scroll)
        self._render()

    def _in(self, box, e):
        return bool(box) and box[0] <= e.x <= box[2] and box[1] <= e.y <= box[3]

    def _on_menu(self, e):
        """右键菜单：位置的保存/复位放这里，不用去找标题栏那枚 chip。

        同小窗：复用 Menu、不在 popup 之后 destroy —— tk_popup 不阻塞，
        紧跟着销毁会让菜单「闪一下就没」，什么也点不了。
        """
        tk = self.app.tk
        p = self.pal
        m = getattr(self, "_menu", None)
        if m is None or not TrayWindow._menu_alive(m):
            m = tk.Menu(self.top, tearoff=0, font=(self.app.family, -self.px(12)),
                        bg=p["card"], fg=p["text"],
                        activebackground=mix(p["card"], p["text"], 0.10),
                        activeforeground=p["text"], bd=0, relief="flat")
            self._menu = m
        m.delete(0, "end")
        m.add_command(label="保存当前布局为默认位置", command=self.save_pos)
        m.add_command(label="恢复默认位置", command=self.reset_pos)
        m.add_separator()
        m.add_command(label="刷新", command=self.refresh)
        m.add_command(label="关闭", command=self.close)
        try:
            m.tk_popup(e.x_root, e.y_root)
        except Exception:
            log("报告窗菜单弹出失败：\n" + __import__("traceback").format_exc())
        finally:
            try:
                m.grab_release()
            except Exception:
                pass

    def refresh(self):
        """重读日志是几百毫秒量级（本机实测 660ms）—— 丢后台线程，
        标题栏在刷新期间不能冻住。

        原来这里是同步调用：点一下 ⟳ 整个窗口僵住两百多毫秒，看着像卡死。
        现在和小窗一样走「后台读 → after 轮询取结果」，期间按钮仍然可点。
        """
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._scan, daemon=True).start()
        self.top.after(80, self._apply_scan)

    def _scan(self):
        try:
            rep = qw.build_report(self.app.args.window, self.app.args.warn)
        except Exception as exc:
            rep = {"models": [], "error": f"读取失败：{exc}"}
        self._scanned = rep
        self._busy = False

    def _apply_scan(self, tries=0):
        try:
            if not self.top.winfo_exists():
                return
        except Exception:
            return
        if self._scanned is not None:
            rep, self._scanned = self._scanned, None
            self.report = rep
            self._resize()
            self._render()
            return
        if self._busy and tries < 80:      # 最多等 6 秒，别无限自排
            self.top.after(80, lambda: self._apply_scan(tries + 1))

    def close(self):
        try:
            self.top.destroy()
        except Exception:
            pass
        self.app._report_win = None


def tkfont_font(tk, family, size, weight="normal"):
    from tkinter import font as tkfont
    return tkfont.Font(family=family, size=size, weight=weight)


def build_parser():
    import argparse
    ap = argparse.ArgumentParser(description="WorkBuddy 配额胶囊 · 右下角常驻小窗")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="不开窗，只把摘要行打到 stdout")
    ap.add_argument("--which", action="store_true", help="列出本机解释器及 tkinter 状态")
    ap.add_argument("--install", action="store_true", help="写入登录启动项，开机后静默待命")
    ap.add_argument("--uninstall", action="store_true", help="移除登录启动项")
    ap.add_argument("--always", action="store_true", help="不跟随 WorkBuddy，直接显示（调试/预览）")
    ap.add_argument("--window", type=int, default=60, help="增速统计窗口（分钟，默认 60）")
    ap.add_argument("--warn", type=float, default=3.0, help="预计触限小时数阈值（默认 3）")
    ap.add_argument("--max-rows", type=int, default=3, help="最多显示几个模型（默认 3）")
    ap.add_argument("--poll", type=float, default=1.5, help="显示状态下探测 WorkBuddy 的间隔秒数")
    ap.add_argument("--poll-idle", type=float, default=5.0,
                    help="--resident 待命（应用已退出）时的探测间隔秒数（默认 5，省点 CPU）")
    ap.add_argument("--refresh", type=float, default=60.0, help="重新读日志的间隔秒数（默认 60）")
    ap.add_argument("--grace", type=float, default=300.0,
                    help="启动后等 WorkBuddy 出现的秒数，超时自行退出（0=一直等）")
    ap.add_argument("--no-topmost", dest="topmost", action="store_false", default=True,
                    help="窗口不置顶")
    ap.add_argument("--exit-on-close", action="store_true",
                    help="关闭主窗口即退出（默认只隐藏，等应用重启再出现）")
    ap.add_argument("--resident", action="store_true",
                    help="WorkBuddy 退出后也不结束，只隐藏，等它再次打开（开机自启用）")
    return ap


def main(argv=None):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    # 探测周期同时是隐藏判定的粒度：低于 0.2 秒没有意义，只会白烧 CPU
    args.poll = max(0.2, float(args.poll or 1.5))
    args.poll_idle = max(args.poll, float(args.poll_idle or 5.0))

    if args.which:
        for exe in python_candidates():
            log(f"{'tk' if _has_tkinter(exe) else '--'}  {exe}")
        return 0
    if args.print_only:
        for line in summary_lines(qw.build_report(args.window, args.warn)):
            log(line)
        return 0
    if args.install:
        ok, msg = install_startup()
        log(msg)
        return 0 if ok else 2
    if args.uninstall:
        ok, msg = uninstall_startup()
        log(msg)
        return 0 if ok else 2

    if os.name != "nt":
        log("小窗依赖 Windows 窗口 API；当前系统请用 python quota_watch.py 看命令行报告。")
        return 2
    try:
        import tkinter  # noqa: F401
    except ImportError:
        log("当前 Python 没有 tkinter，无法显示小窗。")
        log("试试：python quota_tray.py --which   找一个带 tkinter 的解释器。")
        return 3

    if acquire_single_instance() is None:
        log("小窗已在运行。")
        return 0

    enable_dpi_awareness()
    TrayWindow(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
