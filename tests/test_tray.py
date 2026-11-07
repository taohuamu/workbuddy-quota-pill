#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota_tray 纯逻辑回归测试（无 tkinter、不弹窗、不碰真实日志）

覆盖：文本截断、行排序、进度条像素、位置夹取、
      WorkBuddy 进程/窗口状态判定、跟随生命周期状态机。
图形部分无法自动断言，靠人工看；这里只锁死「什么时候该显示/隐藏/退出」。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quota_tray as qt  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def rep(*models):
    return {"models": list(models), "files": 3, "records": 9, "generated_at": "11-06 01:30"}


def m(model, pct, level="ok", eta=None, verdict="按当前节奏够用", shared=None):
    return {"model": model, "percent_used": pct, "percent_left": 100 - pct,
            "level": level, "eta_hours_to_limit": eta, "verdict": verdict,
            "shared_models": shared or [], "reset_local": "11-06 22:38",
            "hours_to_reset": 21.0, "rate_pct_per_hour": 1.0, "rate_confidence": "high"}


def test_text():
    check("ASCII 宽度按 1 计", qt.text_units("abc") == 3)
    check("中文宽度按 2 计", qt.text_units("配额") == 4)
    check("未超长不截断", qt.elide_units("abcdef", 10) == "abcdef")
    out = qt.elide_units("这是一个非常长的模型名字啊啊啊", 10)
    check("超长以省略号结尾", out.endswith("…"), out)
    check("截断后不超宽", qt.text_units(out) <= 10, qt.text_units(out))
    check("零宽度返回空串", qt.elide_units("abc", 0) == "")
    check("极窄只留省略号", qt.elide_units("abcdef", 2) == "…")


def test_theme():
    need = ("card", "edge", "edge_soft", "text", "sub", "track", "hover",
            "shadow", "close_hover_bg", "close_hover", "toast", "level")
    for theme in ("light", "dark"):
        pal = qt.PALETTE[theme]
        check(f"{theme} 主题字段齐全", all(k in pal for k in need),
              [k for k in need if k not in pal])
        check(f"{theme} 四档等级色齐全",
              set(pal["level"]) == {"critical", "warn", "ok", "idle"}, pal["level"])
        check(f"{theme} 阴影三层", len(pal["shadow"]) == 3, pal["shadow"])
        check(f"{theme} 色值合法", all(isinstance(v, str) and v.startswith("#") and len(v) == 7
                                      for k, v in pal.items() if k in ("card", "text", "sub", "track")),
              pal["card"])
    check("混色左端不动", qt.mix("#000000", "#ffffff", 0) == "#000000")
    check("混色右端不动", qt.mix("#000000", "#ffffff", 1) == "#ffffff")
    check("混色中点取中", qt.mix("#000000", "#ffffff", 0.5) == "#808080",
          qt.mix("#000000", "#ffffff", 0.5))
    check("混色越界被夹住",
          qt.mix("#000000", "#ffffff", 9) == "#ffffff" and qt.mix("#000000", "#ffffff", -3) == "#000000")


def test_levels():
    check("空列表按 idle", qt.worst_level([]) == "idle")
    check("ok 与 idle 取 ok", qt.worst_level([{"level": "ok"}, {"level": "idle"}]) == "ok")
    check("含 critical 即 critical",
          qt.worst_level([{"level": "warn"}, {"level": "critical"}]) == "critical")
    check("未知档位不冒头", qt.worst_level([{"level": "ok"}, {"level": "??"}]) == "ok")


def test_pick_rows():
    rows, total = qt.pick_rows(rep(m("a", 10), m("b", 90, "warn"), m("c", 20, "critical")), 2)
    check("按需截断", len(rows) == 2 and total == 3, f"{len(rows)} {total}")
    check("危险优先", rows[0]["model"] == "c", rows[0]["model"])
    check("其次按百分比降序", rows[1]["model"] == "b", rows[1]["model"])
    rows, total = qt.pick_rows({"models": []}, 3)
    check("空报告返回空行", rows == [] and total == 0)


def test_bar():
    check("0% 无填充", qt.bar_fill(0, 200) == 0)
    check("50% 取一半", qt.bar_fill(50, 200) == 100)
    check("1% 至少可见 2px", qt.bar_fill(1, 200) == 2)
    check("100% 满格", qt.bar_fill(100, 200) == 200)
    check("越界值被夹住", qt.bar_fill(180, 200) == 200 and qt.bar_fill(-5, 200) == 0)
    check("脏数据不炸", qt.bar_fill("x", 200) == 0)


def test_mask_and_scroll():
    # 遮罩必须是「全集减去右下角可见区」：收起态胶囊、展开动画每一帧都靠它
    check("满尺寸可见时不需要遮罩", qt.mask_rects(400, 330, 400, 330) == [],
          qt.mask_rects(400, 330, 400, 330))
    rects = qt.mask_rects(400, 330, 200, 60)
    check("胶囊态分成上条 + 左条", rects == [(0, 0, 400, 270), (0, 270, 200, 330)], rects)
    covered = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    check("遮罩面积 = 全集 - 可见区", covered == 400 * 330 - 200 * 60, covered)
    for x0, y0, x1, y1 in rects:      # 任何一块都不能盖住右下角可见区
        overlay = min(x1, 400) - max(x0, 200) if x1 > 200 else 0
        vertical = min(y1, 330) - max(y0, 270) if y1 > 270 else 0
        check("遮罩不侵入可见区", overlay <= 0 or vertical <= 0, (x0, y0, x1, y1))
    check("可见区超界被夹住", qt.mask_rects(400, 330, 999, 999) == [],
          qt.mask_rects(400, 330, 999, 999))
    check("零尺寸可见区全覆盖", qt.mask_rects(100, 80, 0, 0) == [(0, 0, 100, 80)],
          qt.mask_rects(100, 80, 0, 0))

    check("滚动向上越界夹到 0", qt.clamp_scroll(-50, 300) == 0)
    check("滚动向下越界夹到上限", qt.clamp_scroll(999, 300) == 300)
    check("无滚动时恒为 0", qt.clamp_scroll(120, 0) == 0)
    check("区间内原样返回", qt.clamp_scroll(120, 300) == 120)


def test_main_window_predicate():
    # WorkBuddy 进程里有一批 0×0 的 IME/提示窗口，早期被当成主窗口 →
    # 关掉主窗口后小窗永不隐藏，还会浮在微信上面。
    ok = qt.main_window_ok
    check("正常主窗口通过", ok(True, "WorkBuddy", 1500, 900, 0x0, 0, False, False))
    check("0×0 辅助窗口被排除", not ok(True, "HintWnd", 0, 0, 0x0, 0, False, False))
    check("过小窗口被排除", not ok(True, "WorkBuddy", 120, 60, 0x0, 0, False, False))
    check("有 owner 的附属窗口被排除", not ok(True, "Sogou_TSF_UI", 600, 400, 0x0, 77, False, False))
    check("工具窗口被排除", not ok(True, "x", 800, 600, 0x80, 0, False, False))
    check("最小化不算可见", not ok(True, "WorkBuddy", 1500, 900, 0x0, 0, True, False))
    check("被 DWM 隐藏（别的虚拟桌面）不算可见",
          not ok(True, "WorkBuddy", 1500, 900, 0x0, 0, False, True))
    check("无标题被排除", not ok(True, "", 1500, 900, 0x0, 0, False, False))


def test_away_state():
    # 切到别的应用：先留着（可能只是复制点东西），久了才让路
    lc = qt.Lifecycle()                      # 默认 away_after=3
    check("先出现", lc.feed("open", 0) == "show")
    check("刚切走不动", lc.feed("away", 0.5) == "keep")
    check("3 秒内仍留着", lc.feed("away", 2.9) == "keep")
    check("超过阈值让路", lc.feed("away", 3.5) == "hide")
    check("切回来立刻现身", lc.feed("open", 4) == "show")
    check("再切走重新计时", lc.feed("away", 4.5) == "keep")

    lc = qt.Lifecycle(hide_on_away=False)
    lc.feed("open", 0)
    check("关掉开关后一直显示", all(lc.feed("away", 100 + 10 * i) == "keep" for i in range(5)))

    lc = qt.Lifecycle()
    lc.feed("open", 0)
    lc.feed("away", 1)                       # 起算点
    check("到点让路", lc.feed("away", 4.1) == "hide")
    check("让路后继续 away 不重复 hide", lc.feed("away", 9) == "wait")


def test_live_models():
    rep = {"models": [
        {"model": "a", "stale": False, "percent_used": 10},
        {"model": "b", "stale": True, "percent_used": 11},
        {"model": "c", "percent_used": 12},          # 老数据没有 stale 字段 → 视为有效
    ]}
    live = qt.live_models(rep)
    check("过期模型被过滤", [r["model"] for r in live] == ["a", "c"], [r["model"] for r in live])
    check("全部过期时为空", qt.live_models({"models": [{"model": "x", "stale": True}]}) == [])
    check("过期模型不进胶囊", qt.latest_row({"models": [
        {"model": "old", "stale": True, "last_seen": "11-06 23:00"},
        {"model": "new", "stale": False, "last_seen": "11-06 10:00"}]})["model"] == "new")


def test_geom():
    work = (0, 0, 1920, 1040)
    check("区内位置不动", qt.clamp_pos(100, 200, 260, 200, work) == (100, 200))
    check("左边越界拉回", qt.clamp_pos(-50, 200, 260, 200, work)[0] == 0)
    check("右边越界夹住", qt.clamp_pos(5000, 200, 260, 200, work)[0] == 1920 - 260)
    check("下边越界夹住", qt.clamp_pos(100, 5000, 260, 200, work)[1] == 1040 - 200)
    x, y = qt.default_pos(260, 200, work, margin=16)
    check("默认落在右下角", x == 1920 - 260 - 16 and y == 1040 - 200 - 16, f"{x} {y}")
    x, y = qt.default_pos(260, 200, work, margin=16, bottom_gap=60)
    check("可给输入法悬浮栏让位", y == 1040 - 200 - 60, y)
    small = (0, 0, 200, 150)
    check("工作区比窗口小时不返回负数", qt.default_pos(260, 200, small, 10) == (0, 0))

    check("矩形按比例缩放（150% 屏）",
          qt.scale_rect((0, 0, 2560, 1528), 1707 / 2560.0) == (0, 0, 1707, 1018),
          qt.scale_rect((0, 0, 2560, 1528), 1707 / 2560.0))
    check("比例为 1 时原样返回",
          qt.scale_rect((0, 0, 2560, 1528), 1.0) == (0, 0, 2560, 1528))

    dx, dy = qt.corner_offset(1441, 889, 266, 130, work)
    check("偏移按右下角起算", (dx, dy) == (213, 21), (dx, dy))
    check("偏移可还原位置", qt.pos_from_corner(dx, dy, 266, 130, work) == (1441, 889))
    check("负偏移被夹到贴边",
          qt.pos_from_corner(-50, -50, 266, 130, work) == (1920 - 266, 1040 - 130))
    check("偏移超出屏幕仍夹在区内",
          qt.pos_from_corner(9999, 9999, 266, 130, work) == (0, 0))
    big = (0, 0, 2560, 1528)
    x2, y2 = qt.pos_from_corner(213, 21, 266, 130, big)
    check("换分辨率后仍贴右下角", (x2, y2) == (2560 - 266 - 213, 1528 - 130 - 21), (x2, y2))

    # 胶囊态跟小窗：卡片与胶囊共用同一个右下角锚点，换尺寸时右下角不动
    dx, dy = qt.corner_offset(1707 - 266, 1019 - 130, 266, 130, work)
    px_, py_ = qt.pos_from_corner(dx, dy, 120, 34, work)
    check("胶囊与小窗共用右下角", (px_ + 120, py_ + 34) == (1707, 1019),
          (px_ + 120, py_ + 34))


def test_process():
    check("主进程识别", qt.is_wb_exe("WorkBuddy.exe"))
    check("辅助进程识别", qt.is_wb_exe("WorkBuddyX.exe"))
    check("带路径也识别", qt.is_wb_exe(r"C:\Program Files\WorkBuddy\workbuddy.exe"))
    check("大小写无关", qt.is_wb_exe("WORKBUDDY.EXE"))
    check("无关进程不误判", not qt.is_wb_exe("notepad.exe") and not qt.is_wb_exe(""))
    check("没有进程判为 gone", qt.app_state_from(set(), set()) == "gone")
    check("有进程有窗口判为 open", qt.app_state_from({100}, {100}) == "open")
    check("有进程无窗口判为 hidden", qt.app_state_from({100}, set()) == "hidden")
    check("窗口属于别的进程仍算 hidden", qt.app_state_from({100}, {200}) == "hidden")


def test_rows():
    name, pct, sub = qt.row_texts(m("m1", 12))
    check("名称与百分比", name == "m1" and pct == "12%", f"{name} {pct}")
    _, _, sub = qt.row_texts(m("m1", 90, "critical", eta=2.0))
    check("危险行提示重置前耗尽", "重置前耗尽" in sub, sub)
    _, _, sub = qt.row_texts(m("m1", 96, "critical", eta=2.0))
    check("已到上限时改口为已满", "已到上限" in sub and "重置前耗尽" not in sub, sub)
    _, _, sub = qt.row_texts(m("m1", 40, "ok", eta=2.5))
    check("有预测时给触限时间", sub.startswith("预计") and "小时" in sub, sub)
    _, _, sub = qt.row_texts(m("m1", 40, "idle", eta=None, verdict="样本不足，无法预测"))
    check("无预测时退回验语", sub == "样本不足，无法预测", sub)
    _, _, sub = qt.row_texts(m("m1", 40, "ok", eta=5.0, shared=["m2", "m3"]))
    check("共用配额会标注", "2 个模型共用" in sub, sub)


def test_summary():
    lines = qt.summary_lines(rep(m("a", 12), m("b", 88, "warn")))
    check("每模型一行", len(lines) == 2, lines)
    check("长模型名先丢能力后缀", qt.short_model("deepseek-v4.1-flash") == "deepseek-v4.1",
          qt.short_model("deepseek-v4.1-flash"))
    check("短模型名不动", qt.short_model("gpt-5.1") == "gpt-5.1")
    check("胶囊取最近用的模型",
          qt.latest_row({"models": [{"model": "a", "last_seen": "11-06 10:00"},
                                    {"model": "b", "last_seen": "11-06 13:20"}]})["model"] == "b")
    check("没有模型时返回 None", qt.latest_row({"models": []}) is None)
    check("摘要含档位与百分比", " 12% · " in lines[0] and lines[0].startswith("[正常]"), lines[0])
    check("摘要与小窗共用判决语", lines[0].endswith("按当前节奏够用"), lines[0])
    check("摘要时间无多余空格", "小时后触限" in qt.summary_lines(
        rep(m("a", 30, "warn", eta=2.0)))[0], lines)
    err = qt.summary_lines({"models": [], "error": "没数据"})
    check("错误走单行", len(err) == 1 and "没数据" in err[0], err)
    check("空模型不返回空列表", qt.summary_lines({"models": []}) == ["配额哨兵：暂无数据"])

    t1, t2 = qt.error_texts("未找到配额快照（是否还没用 WorkBuddy 跑过对话？）")
    check("空数据错误被压成短句", t1 == "还没有配额快照" and "自动出现" in t2, (t1, t2))
    t1, t2 = qt.error_texts("读取失败：权限不足" * 5)
    check("长错误两行都不超宽",
          qt.text_units(t1) <= 26 and qt.text_units(t2) <= 26, (t1, t2))


def test_startup():
    tmp = tempfile.mkdtemp(prefix="qw-boot-")
    old = os.environ.get("APPDATA")
    try:
        os.environ["APPDATA"] = tmp
        ok, msg = qt.install_startup()
        if ok:
            p = qt.startup_file()
            raw = open(p, "rb").read()
            try:
                body = raw.decode("mbcs", "ignore")
            except LookupError:
                body = raw.decode("utf-8", "ignore")
            check("启动项写进 Startup 目录",
                  p.startswith(tmp) and p.endswith(qt.STARTUP_NAME), p)
            check("启动项指向小窗脚本", "quota_tray.py" in body, body[:60])
            check("启动项用无控制台解释器", "pythonw" in body.lower(), body[:60])
            check("启动项是看门狗而非直接拉起",
                  "Win32_Process" in body and "--resident" not in body, body[:120])
            check("看门狗只为 WorkBuddy 起停",
                  "WorkBuddy.exe" in body and "--grace 120" in body, body[:160])
            check("安装后状态可识别", qt.startup_installed())
            ok2, _ = qt.uninstall_startup()
            check("启动项可撤销", ok2 and not qt.startup_installed())
        else:
            check("缺 tkinter 时给出可读原因", "tkinter" in msg, msg)
    finally:
        if old is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle():
    # 不跟随：立刻显示，之后一直保持
    lc = qt.Lifecycle(follow=False)
    check("--always 立即显示", lc.feed("gone", 1000) == "show")
    check("--always 不退出", lc.feed("gone", 99999) == "keep")

    # 启动后一直没见到 WorkBuddy：宽限期到就退出
    lc = qt.Lifecycle(grace=300)
    check("未见应用时先等待", lc.feed("gone", 0) == "wait")
    check("宽限期内仍等待", lc.feed("gone", 280) == "wait")
    check("宽限期满退出", lc.feed("gone", 301) == "exit")

    lc = qt.Lifecycle(grace=0)
    check("grace=0 时长期待命", lc.feed("gone", 10 ** 6) == "wait")

    # 打开 → 显示；关闭主窗口 → 隐藏；再打开 → 重新出现
    lc = qt.Lifecycle()
    check("WorkBuddy 打开即出现", lc.feed("open", 0) == "show")
    check("持续打开不重复动作", lc.feed("open", 1) == "keep")
    check("关窗瞬间先不隐藏", lc.feed("hidden", 2) == "keep")
    check("1.5 秒内仍留着", lc.feed("hidden", 3.4) == "keep")
    check("关窗超时后隐藏", lc.feed("hidden", 3.6) == "hide")
    check("重新打开再现身", lc.feed("open", 9) == "show")

    # 进程真退出：1.5 秒后隐藏，10 秒后收工
    lc = qt.Lifecycle()
    lc.feed("open", 0)
    check("退出瞬间仍保留", lc.feed("gone", 2) == "keep")
    check("1.5 秒内仍未隐藏", lc.feed("gone", 3.4) == "keep")
    check("先隐藏再退出", lc.feed("gone", 3.6) == "hide")
    check("宽限后进程结束", lc.feed("gone", 20) == "exit")

    # 应用重启（短暂消失）不该把小窗带走
    lc = qt.Lifecycle()
    lc.feed("open", 0)
    lc.feed("gone", 1)
    check("重启间隙不退出", lc.feed("gone", 4) != "exit")
    check("重启后继续工作", lc.feed("open", 5) == "show")

    # resident：应用退出也不结束，只隐藏，等它再次打开
    lc = qt.Lifecycle(stay=True)
    lc.feed("open", 0)
    check("resident 下进程退出先保留", lc.feed("gone", 60) == "keep")
    check("resident 下 1.5 秒后隐藏", lc.feed("gone", 62) == "hide")
    check("resident 长期待命", all(lc.feed("gone", 60 + 10 * i) != "exit" for i in range(1, 20)))
    check("resident 重开再现身", lc.feed("open", 400) == "show")
    lc = qt.Lifecycle(stay=True, grace=1)
    check("resident 不受宽限期约束", lc.feed("gone", 10 ** 4) == "wait")

    # 严格模式：关窗即退出
    lc = qt.Lifecycle(exit_on_close=True)
    lc.feed("open", 0)
    lc.feed("hidden", 1)
    check("exit_on_close 关窗即退出", lc.feed("hidden", 5) == "exit")


def test_state_file():
    tmp = tempfile.mkdtemp(prefix="qw-tray-")
    old_dir, old_file = qt.STATE_DIR, qt.STATE_FILE
    try:
        qt.STATE_DIR = tmp
        qt.STATE_FILE = os.path.join(tmp, "tray.json")
        check("无状态文件返回空 dict", qt.load_state() == {})
        qt.save_state({"dx": 213, "dy": 21, "topmost": False})
        got = qt.load_state()
        check("偏移可回写", got.get("dx") == 213 and got.get("dy") == 21, got)
        check("置顶开关可回写", got.get("topmost") is False, got)
        with open(qt.STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write("{坏掉的 json")
        check("坏文件当成空状态", qt.load_state() == {})
    finally:
        qt.STATE_DIR, qt.STATE_FILE = old_dir, old_file
        shutil.rmtree(tmp, ignore_errors=True)


def test_layout_state():
    """布局存档：layout 子树优先、旧平铺字段兼容、保存时清掉旧字段。"""
    tmp = tempfile.mkdtemp(prefix="qw-lay-")
    old_dir, old_file = qt.STATE_DIR, qt.STATE_FILE
    try:
        qt.STATE_DIR = tmp
        qt.STATE_FILE = os.path.join(tmp, "tray.json")
        check("无存档时 layout 为空", qt.layout_part("tray") == {})
        qt.save_layout({"tray": {"dx": 20, "dy": 34}})
        check("写入后可读", qt.layout_part("tray") == {"dx": 20, "dy": 34})
        check("没写过的分支为空", qt.layout_part("report") == {})
        qt.save_layout({"report": {"dx": 40, "dy": 60}})
        check("两次保存互不覆盖",
              qt.layout_part("tray")["dx"] == 20 and qt.layout_part("report")["dy"] == 60)
        st = qt.load_state()
        check("旧的平铺字段被清掉", "dx" not in st and "report_dx" not in st)
        check("带保存时间戳", bool(st["layout"].get("saved_at")))

        # 老版本存档：只有平铺 dx/dy，没有 layout 键 —— 必须还能读出来
        qt.save_state({"dx": 7, "dy": 9, "report_dx": 3, "report_dy": 5})
        check("兼容旧存档 tray", qt.layout_part("tray") == {"dx": 7, "dy": 9})
        check("兼容旧存档 report", qt.layout_part("report") == {"dx": 3, "dy": 5})

        # 只保存一侧时，另一侧的旧字段必须留住 ——
        # 踩过的坑：保存小窗顺手清了 report_dx/dy，大窗从此只能回默认落点
        qt.save_layout({"tray": {"dx": 1, "dy": 2}})
        st2 = qt.load_state()
        check("保存小窗不动大窗旧字段",
              st2.get("report_dx") == 3 and st2.get("report_dy") == 5, st2)
        check("大窗仍能读回旧位置", qt.layout_part("report") == {"dx": 3, "dy": 5})
        check("已迁移的旧键被清掉", "dx" not in st2 and "dy" not in st2, st2)

        qt.clear_layout()
        check("清空后两边都空",
              qt.layout_part("tray") == {} and qt.layout_part("report") == {})
    finally:
        qt.STATE_DIR, qt.STATE_FILE = old_dir, old_file
        shutil.rmtree(tmp, ignore_errors=True)


def test_wb_pid_cache():
    """进程探测缓存：命中不重扫、pid 失效/TTL 到期必须重扫。"""
    qt._WB_PID_CACHE["pids"] = frozenset()
    qt._WB_PID_CACHE["ts"] = 0.0
    ids = qt.wb_process_ids(force=True)
    check("全量扫描后回填缓存", qt._WB_PID_CACHE["ts"] > 0)
    if ids:
        ts = qt._WB_PID_CACHE["ts"]
        qt.wb_process_ids(now=ts + 1.0)
        check("缓存期内不重扫", qt._WB_PID_CACHE["ts"] == ts)
        qt.wb_process_ids(now=ts + qt.WB_RESCAN_EVERY + 5)
        check("超过 TTL 强制重扫", qt._WB_PID_CACHE["ts"] > ts)
    else:
        check("没装 WorkBuddy 时返回空集", ids == set())
    # pid 复用/退出：缓存里的假 pid 必须触发重扫，否则会一直判 gone 或误判 open
    qt._WB_PID_CACHE["pids"] = frozenset({-1})
    qt._WB_PID_CACHE["ts"] = time.time()
    qt.wb_process_ids()
    check("缓存 pid 失效时重扫", qt._WB_PID_CACHE["pids"] != frozenset({-1}))
    qt._WB_PID_CACHE["pids"] = frozenset()
    qt._WB_PID_CACHE["ts"] = 0.0


def test_latest_row():
    """跨年不能按 "MM-DD HH:MM" 字符串比大小 —— 12-31 会大于 01-01。"""
    rows = [
        {"model": "old", "last_seen": "12-31 23:50", "last_seen_ts": 1767229800.0,
         "requests": 5, "percent_used": 10, "level": "ok"},
        {"model": "new", "last_seen": "01-01 00:10", "last_seen_ts": 1767231000.0,
         "requests": 2, "percent_used": 11, "level": "ok"},
    ]
    got = qt.latest_row({"models": rows})
    check("跨年按时间戳选最近", got["model"] == "new", got["model"])

    legacy = [{k: v for k, v in r.items() if k != "last_seen_ts"} for r in rows]
    got = qt.latest_row({"models": legacy})
    check("缺数值时间戳时退回字符串比较", got["model"] == "old", got["model"])
    check("空报告返回 None", qt.latest_row({"models": []}) is None)


if __name__ == "__main__":
    print("quota-tray 逻辑测试")
    test_text()
    test_theme()
    test_levels()
    test_pick_rows()
    test_bar()
    test_mask_and_scroll()
    test_main_window_predicate()
    test_away_state()
    test_live_models()
    test_geom()
    test_process()
    test_rows()
    test_summary()
    test_startup()
    test_lifecycle()
    test_state_file()
    test_layout_state()
    test_wb_pid_cache()
    test_latest_row()
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
