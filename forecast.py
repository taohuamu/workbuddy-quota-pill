#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「最近 15 分钟涨了几格」换成「按这个速度还要多久」。

为什么不用端点差分
------------------
`quota_watch.burn_rate()` 用「窗口首末两点相减 ÷ 时长」。15 分钟里可能有 40 条快照，
端点差分只用了其中 2 条：跳一格就是 4 格/小时，量化误差 100%。而且首点高、末点低
时它会算出负贡献，碰上服务端的 ±1 回退就失真。

这里改成数**净增格数** k：窗口内每一次正向跳变累加幅度，回退记 0。
k 是泊松过程在窗口内的充分统计量，量化误差只有 1/k。

判定与读数分开
--------------
- `alert`：要不要在 80% 之前提前提醒。用 χ² 单侧下界 λ_L ≥ (100-p)/HORIZON，
  单次误报率 ALPHA。这是唯一的布尔量。
- `eta_min`：按当前速度走到 100% 的中位时间，只用于展示，不参与判定。

中位时间不取 R/(k/W)：那是凸变换，Jensen 会让它偏大，k 越小偏得越狠。
改用 Jeffreys 先验下的精确边际：λ|k ~ Gamma(k+1/2, W)，T|λ ~ Gamma(R, λ)，
于是 T/(T+W) ~ Beta(R, k+1/2)，取 Beta 分位反解 T。

只用标准库（math + bisect），无第三方依赖。
"""
import bisect
import math
import time

WINDOW_MIN = 15.0      # 观测窗：只看最近这么久
HORIZON_H = 2.0        # 预测时限：多少小时内会到 100% 才算「值得提前提醒」
ALPHA = 0.20           # 单次判定误报率（0.20 见 README「为什么不是 0.05」）
FLOOR_PCT = 55.0       # 低于此百分比不做提前提醒：剩余太多，两小时外推最不可靠
GATE_PCT = 80.0        # 到这个百分比交给「已用 ≥N%」条件，不重复判
TIER_FEW = 4           # k 少于这个数只给主数，不给区间（区间会宽到 7 倍以上）


# ---------------------------------------------------------------------------
# 观测：窗口内净增格数
# ---------------------------------------------------------------------------
def _norm(rows):
    """接受 [{ts,percent}] 或 [(ts,percent)]，返回按时间升序的 [(ts,percent)]。"""
    out = []
    for r in rows or ():
        if isinstance(r, dict):
            t, p = r.get("ts"), r.get("percent")
        else:
            t, p = r[0], r[1]
        if t is None or p is None:
            continue
        out.append((float(t), float(p)))
    out.sort()
    return out


def grids_in(series, lo, hi):
    """(lo, hi] 内的净增格数：只累加正增量，回退记 0，多格跳变按格计权。

    返回 (格数, 事件数)。事件数用于复合跳变守卫：若一格算一次会让「跳 3 格」
    被当成三次独立证据，方差被低估。
    """
    if not series:
        return 0.0, 0
    ts = [x[0] for x in series]
    i = bisect.bisect_right(ts, lo)
    j = bisect.bisect_right(ts, hi)
    cells = 0.0
    events = 0
    prev = series[max(0, i - 1)][1] if i > 0 else None
    for idx in range(i, j):
        p = series[idx][1]
        if prev is not None and p > prev:
            cells += p - prev
            events += 1
        prev = p
    return cells, events


def cells_of(series, lo, hi):
    """带复合跳变守卫的格数：格数超过事件数 1.2 倍时退化为事件计数。"""
    cells, events = grids_in(series, lo, hi)
    if events and cells > 1.2 * events:
        return events, events
    return cells, events


# ---------------------------------------------------------------------------
# χ² 下分位（标准库实现：不完全伽马 + 二分求逆）
# ---------------------------------------------------------------------------
def _gammainc_p(a, x):
    """正则化下不完全伽马 P(a,x)。"""
    if x <= 0:
        return 0.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-15:
                break
        return s * math.exp(-x + a * math.log(x) - gln)
    # 连分式求 Q(a,x)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return 1.0 - math.exp(-x + a * math.log(x) - gln) * h


def chi2_cdf(x, df):
    return _gammainc_p(df / 2.0, x / 2.0)


def chi2_ppf(q, df):
    """χ² 分布的 q 分位，二分求逆。"""
    lo, hi = 0.0, df + 10.0 * math.sqrt(4.0 * df) + 50.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if chi2_cdf(mid, df) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def rate_lcb(k, window_h, alpha=ALPHA):
    """泊松计数 k 在窗口 width_h 内的速率单侧下界（格/小时）。"""
    if k <= 0:
        return 0.0
    return chi2_ppf(alpha, 2.0 * k) / (2.0 * window_h)


def k_crit(need_per_h, window_h, alpha=ALPHA):
    """要让 λ_L ≥ need 至少需要几格。"""
    k = 1
    while k < 200 and rate_lcb(k, window_h, alpha) < need_per_h:
        k += 1
    return k


# ---------------------------------------------------------------------------
# Beta 分位：把 k 换成「还要多久」的分位
# ---------------------------------------------------------------------------
def _betacf(a, b, x):
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def betai(a, b, x):
    """正则化不完全贝塔 I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def inv_betai(prob, a, b):
    """I_x(a,b) = prob 的 x，二分求逆。"""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if betai(a, b, mid) < prob:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def t_quantile(q, need, k, window_min=WINDOW_MIN):
    """还剩 need 格时，按窗口内 k 格的速度，走完所需时间（分钟）的 q 分位。"""
    if need <= 0:
        return 0.0
    a, b = float(need), float(k) + 0.5
    x = inv_betai(q, a, b)
    if x >= 1.0:
        return float("inf")
    return window_min * x / (1.0 - x)


# ---------------------------------------------------------------------------
# 一次判定
# ---------------------------------------------------------------------------
def decide(rows, now=None, window_min=WINDOW_MIN, horizon_h=HORIZON_H,
           alpha=ALPHA, pct_threshold=GATE_PCT, floor_pct=FLOOR_PCT,
           hours_to_reset=None):
    """返回一个 dict。`alert` 是唯一的布尔量；其余都是给人看的读数。

    rows            : 本周期快照（[{ts,percent}] 或 [(ts,percent)]），任意模型
    now             : 判定时刻，None = 最后一条快照
    hours_to_reset  : 距本周期重置还有几小时；给 None 就不做「本周期内到不了」的判断
    """
    window_min = max(1.0, float(window_min))   # 0 或负数没有意义，且会让速率除零
    series = _norm(rows)
    blank = {"alert": False, "p": None, "k": 0, "need": None, "eta_min": None,
             "tier": "none", "capped": False, "why": "无快照",
             "window_min": float(window_min)}
    if not series:
        return blank
    if now is None:
        now = series[-1][0]
    hist = [x for x in series if x[0] <= now]
    if not hist:
        blank.update({"why": "判定时刻之前无快照"})
        return blank

    p = hist[-1][1]
    need = 100.0 - p
    w_h = window_min / 60.0
    # window_min 随结果一起带出去：展示层（卡片、--print）只拿到这个字典，
    # 不给它窗口就只能按默认值说话，--window 一改文案就对不上读数。
    out = {"alert": False, "p": p, "k": 0, "need": need, "eta_min": None,
           "tier": "none", "capped": False, "why": "",
           "window_min": float(window_min)}

    if p >= 100:
        out.update({"tier": "done", "why": "额度已耗尽"})
        return out
    if hours_to_reset is not None and hours_to_reset * 60.0 < window_min:
        # 单独一个 tier：说「数据不足」是错的，快照其实很全，只是这个周期快结束了，
        # 15 分钟的窗口已经跨过重置边界，数出来的格子会混进下一个周期。
        out.update({"tier": "soon", "why": "距重置不足一个窗口"})
        return out

    k, n_ev = cells_of(hist, now - window_min * 60.0, now)
    out["k"] = k
    if k <= 0:
        out.update({"tier": "idle", "why": "窗口内无新增"})
        return out

    # 读数：中位时间（只用于展示）
    eta = t_quantile(0.5, need, k, window_min)
    out["eta_min"] = eta
    out["tier"] = "few" if k < TIER_FEW else "ok"
    if hours_to_reset is not None and eta > hours_to_reset * 60.0:
        out["capped"] = True
        out["why"] = "中位时间晚于重置"

    # 判定：唯一的布尔量
    if p < pct_threshold and p >= floor_pct:
        lcb = rate_lcb(k, w_h, alpha)
        if lcb >= need / horizon_h:
            out["alert"] = True
            out["why"] = "λ_L=%.1f ≥ 门槛 %.1f" % (lcb, need / horizon_h)
        elif not out["why"]:
            out["why"] = "λ_L=%.1f < 门槛 %.1f" % (lcb, need / horizon_h)
    elif p >= pct_threshold:
        out["why"] = "已过 %.0f%%，交给百分比条件" % pct_threshold
    else:
        out["why"] = "低于 %.0f%%，不提前提醒" % floor_pct
    return out


# ---------------------------------------------------------------------------
# 等级：只由百分比决定
# ---------------------------------------------------------------------------
LEVEL_RANK = {"critical": 0, "warn": 1, "ok": 2, "idle": 3}


def level_of(pct):
    """等级只由已用百分比决定，与速率无关。

    旧口径里有一条「低置信度且 ≥80% → critical，按最坏打算」，那是端点差分时代的
    补丁：预测不出来就当成坏消息。现在预测总是有的（哪怕是「无新增」），这条补丁
    没有存在理由，留着只会让「按最坏打算」这种旧说法漏到屏幕上。
    """
    if pct >= 95:
        return "critical"
    if pct >= GATE_PCT:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# 文案：只给结果，不报依据
# ---------------------------------------------------------------------------
def dur_minutes(minutes):
    """47 分钟 / 2.5 小时 / 1.2 天。"""
    if minutes is None:
        return "—"
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 90:
        return "%d 分钟" % int(round(minutes))
    if minutes < 48 * 60:
        h = minutes / 60.0
        return ("%.0f 小时" % h) if abs(h - round(h)) < 0.05 else ("%.1f 小时" % h)
    return "%.1f 天" % (minutes / 1440.0)


def phrase(fc, window_min=WINDOW_MIN, brief=False):
    """结果句。不含格数、不含速率、不含「1 小时内」这类承诺。

    brief=True 去掉「按此速度」四个字，给胶囊卡片用——那里一行只有两百多像素，
    长模型名本来就会把副标题挤到第二行。
    """
    tier = (fc or {}).get("tier")
    if tier == "idle":
        # 用显式 None 判断而不是 `or`：window_min=0 会被 `or` 当成假值，回落到默认 15。
        wm = (fc or {}).get("window_min")
        wm = window_min if wm is None else wm
        return "近 %d 分钟无新增" % int(wm)
    if tier == "done":
        return "已到上限"
    if tier == "soon":
        return "即将重置"
    if tier == "none":
        return "数据不足"
    eta = fc.get("eta_min")
    if eta is None:
        return "数据不足"
    if fc.get("capped"):
        return "本周期内到不了上限"
    if brief:
        return "约 %s后到上限" % dur_minutes(eta)
    return "按此速度约 %s后到上限" % dur_minutes(eta)


def say(model, percent, fc, window_min=WINDOW_MIN):
    """「模型 已用 N%，结果」——报告与通知共用这一句。"""
    return "%s 已用 %d%%，%s" % (model, int(round(percent)), phrase(fc, window_min))


def obs_cell(fc):
    """表格「近 N 分钟」列。

    对外只说「涨了几个百分点」——内部计数的单位就是百分比点（percent 是整数，1 格 = 1%），
    没必要把「格」这个内部术语漏到界面上。零增量照实写「涨 0%」：换成「持平」是替读者
    做了一次解释，而 0 这个数字本身没有歧义。
    """
    if (fc or {}).get("tier") in ("none", "soon"):
        return "—"          # 没有窗口可数，此时写 0% 才是误导
    return "涨 %d%%" % int((fc or {}).get("k") or 0)


def eta_cell(fc, window_min=WINDOW_MIN):
    """表格「按此速度」列。"""
    tier = (fc or {}).get("tier")
    if tier == "soon":
        return "即将重置"
    if tier in ("idle", "none"):
        return "—"
    if tier == "done":
        return "已到上限"
    eta = fc.get("eta_min")
    if eta is None:
        return "—"
    if fc.get("capped"):
        # 桌面端是「标签 + 值」成对显示，前缀「按此速度」已在标签里，
        # 值再写「本周期内到不了」会读成「按此速度 本周期内到不了」。
        return "到不了上限"
    txt = "约 %s" % dur_minutes(eta)
    return txt + ("（样本少）" if tier == "few" else "")


def should_notify(m, pct=GATE_PCT):
    """这个模型是否该进通知/注意行：已用达标，或 forecast 判定值得提前提醒。"""
    if (m.get("percent_used") or 0) >= pct:
        return True
    return bool((m.get("forecast") or {}).get("alert"))


def attach(models, snaps, window_min=WINDOW_MIN, horizon_h=HORIZON_H,
           floor_pct=FLOOR_PCT, pct=GATE_PCT, now=None):
    """给每个在用的模型挂上 forecast，并让 level / verdict 只由百分比决定。

    报告层和通知层都调这一个，两处等级才不会分叉。
    为什么要重算 level：数据层的 level 里还有一条「低置信度且 ≥80% → critical，
    按最坏打算」，那是旧的端点差分口径。新口径下没有「置信度」这个概念，留着会让
    报告冒出「按最坏打算」这种旧说法，也会让冷却键按旧等级走。

    `now` 默认取**当前时刻**，不是最后一条快照：一个模型三小时没动过，窗口就该是空的
    （「近 15 分钟无新增」），而不是把它三小时前那阵爆发重新算一遍报给你。
    """
    w_now = time.time() if now is None else now
    for m in models:
        if m.get("stale"):
            continue
        rows = snaps.get(m["model"]) or []
        cs = m.get("cycle_start")
        if cs:
            rows = [r for r in rows if r["ts"] >= cs - 1.0]
        fc = decide(rows, now=w_now, window_min=window_min, horizon_h=horizon_h,
                    floor_pct=floor_pct, pct_threshold=pct,
                    hours_to_reset=m.get("hours_to_reset"))
        m["forecast"] = fc
        m["level"] = level_of(m["percent_used"])
        m["verdict"] = phrase(fc, window_min)
    models.sort(key=lambda r: (LEVEL_RANK.get(r["level"], 9),
                               -(r["percent_used"] or 0)))
    return models


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selftest():
    ok = 0
    fail = []

    def chk(name, cond, got=""):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fail.append("%s（实际 %s）" % (name, got))

    # χ² 分位与常用表比对（α=0.05，df=2k）
    ref = {2: 0.103, 4: 0.711, 6: 1.635, 8: 2.733, 10: 3.940, 12: 5.226}
    err = max(abs(chi2_ppf(0.05, df) - v) for df, v in ref.items())
    chk("χ² 分位误差 < 1e-3", err < 1e-3, "%.2e" % err)

    # λ_L 单调
    vals = [rate_lcb(k, 0.25) for k in range(1, 13)]
    chk("λ_L 随 k 严格递增", all(b > a for a, b in zip(vals, vals[1:])))
    chk("λ_L(k=12,α=.2)≈36.12", abs(rate_lcb(12, 0.25, 0.20) - 36.12) < 0.01,
        "%.3f" % rate_lcb(12, 0.25, 0.20))

    # 计数：回退不算负贡献
    s = [(0, 50), (60, 51), (120, 50), (180, 52)]
    chk("回退记 0、跳 2 格记 2（裸计数）", grids_in(s, -1, 200)[0] == 3, grids_in(s, -1, 200)[0])
    chk("复合跳变守卫退化为事件数", cells_of(s, -1, 200)[0] == 2, cells_of(s, -1, 200)[0])

    # Beta 分位：p=71,k=9 → 中位约 47 分钟（与 supplement.py 对照）
    t = t_quantile(0.5, 29, 9)
    chk("p=71 k=9 中位≈47 分钟", 45 <= t <= 49, "%.1f" % t)

    # 判定：人造爆发
    rows = [(i * 60, min(72, 60 + i)) for i in range(13)]   # 60% → 72%，12 分钟 12 格
    fc = decide(rows, now=rows[-1][0], hours_to_reset=10)
    chk("12 格/12 分钟 → 提前提醒", fc["alert"] is True, fc)
    # 慢速不该响
    rows2 = [(i * 300, 60 + i * 0.5) for i in range(5)]
    chk("慢速不提醒", decide(rows2, hours_to_reset=10)["alert"] is False)
    # 低于地板不响
    rows3 = [(i * 60, 20 + i) for i in range(13)]
    chk("p<55 不提醒", decide(rows3, hours_to_reset=10)["alert"] is False)
    # 过了 80% 让位
    rows4 = [(i * 60, 78 + i) for i in range(13)]
    chk("p≥80 让位", decide(rows4, hours_to_reset=10)["alert"] is False)
    # 空窗口
    chk("无新增 → idle", decide([(0, 50), (3600, 50)], hours_to_reset=10)["tier"] == "idle")

    # 文案：不含格数/速率
    txt = say("m", 84, {"tier": "ok", "eta_min": 40, "capped": False})
    chk("正文不含格数", "格" not in txt, txt)
    chk("正文不含 /小时", "小时" not in txt.replace("40 分钟", ""), txt)
    print("自检：通过 %d，失败 %d" % (ok, len(fail)))
    for f in fail:
        print("  [FAIL]", f)
    return 0 if not fail else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
