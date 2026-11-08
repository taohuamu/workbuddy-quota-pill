#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 模型配额胶囊
=====================
从本机 WorkBuddy 会话记录中读取**官方下发的实时配额快照**
(providerData.rawUsage.quota)，按模型汇总并推算触限时间，
用于在额度耗尽、打断工作流之前提前换模型或收尾。

数据来源（只读，不上传）：
    %USERPROFILE%\\.workbuddy\\projects\\*\\*.jsonl

用法：
    python quota_watch.py              # 表格报告
    python quota_watch.py --json       # 机器可读，便于接自动化
    python quota_watch.py --window 120 # 用最近 120 分钟算增速
    python quota_watch.py --warn-only  # 仅在有风险时输出（配合定时任务）

退出码：
    0 = 全部安全    1 = 有模型存在触限风险（>warn 阈值）
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict

PERIOD_H = 24.0      # 配额池周期（小时）：reset_at 往前推一天 = 本周期起点
RESET_GRACE = 60.0   # 重置时刻前后 60 秒的抖动宽限

HOME = os.environ.get("WORKBUDDY_HOME") or os.path.join(os.path.expanduser("~"), ".workbuddy")
PROJECTS = os.path.join(HOME, "projects")


def norm_reset(v):
    """重置时间：秒或毫秒时间戳 → 秒级 float。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 1e11 else v


def as_num(value, cast=float, default=None):
    """日志字段 → 数值；转不动返回 default。

    日志是客户端写的，类型会变：percent 可能带百分号、tokens 可能带千分位。
    任何一处转不动都不该让整份报告失效。
    """
    if value is None or isinstance(value, bool):
        return default
    for candidate in (value, str(value).replace(",", "").replace("%", "").strip()):
        try:
            return cast(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
    return default


def as_epoch(value):
    """timestamp → 秒级 float。容忍毫秒数、秒数、ISO 8601 字符串；不行返回 None。"""
    num = as_num(value)
    if num is not None:
        return num / 1000.0 if num > 1e11 else float(num)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _snapshot_of(rec):
    """一行日志 → (model, 快照)；不是配额行、或关键字段坏掉 → None。

    调用方把整段解析包在 try 里：宁可丢掉这一行，也不要把异常抛到
    build_report，让整份报告变成「读取失败」——一条坏行不该废掉整个工具。
    """
    if not isinstance(rec, dict):
        return None
    pd = rec.get("providerData")
    if not isinstance(pd, dict):
        return None
    ru = pd.get("rawUsage")
    if not isinstance(ru, dict):
        return None
    q = ru.get("quota")
    if not isinstance(q, dict):
        return None
    ts = as_epoch(rec.get("timestamp"))
    percent = as_num(q.get("percent"))
    if ts is None or percent is None:
        return None
    shared = q.get("shared_models")
    if not isinstance(shared, list):
        shared = []
    return (q.get("model") or pd.get("model") or "unknown"), {
        "ts": ts,
        "percent": percent,
        "reset_at": norm_reset(q.get("reset_at")),
        "shared": shared,
        "credit": as_num(ru.get("credit"), default=0.0),
        "tokens": as_num(ru.get("total_tokens") or ru.get("totalTokens"),
                         cast=int, default=0),
    }


def load_snapshots():
    """返回 ({model: [ {ts,percent,reset_at,credit,tokens} ... ]}, 文件数, 快照数)。"""
    snaps = defaultdict(list)
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    seen = 0
    for path in files:
        try:
            fh = open(path, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"quota"' not in line:
                    continue
                try:
                    got = _snapshot_of(json.loads(line))
                except Exception:
                    continue            # 单行任何异常都不许外溢
                if got is None:
                    continue
                model, row = got
                snaps[model].append(row)
                seen += 1
    for m in snaps:
        snaps[m].sort(key=lambda r: r["ts"])
    return snaps, len(files), seen


def burn_rate(rows, window_min):
    """近 window_min 分钟内配额百分比的增长速率。

    返回 (rate_pct_per_hour, confidence, span_min, delta_pct)；
    速率不可得时 rate 为 None。confidence ∈ {high, medium, low}。
    配额百分比是整数，样本跨度太短会把速率放大好几倍，因此按跨度分档。
    """
    now = rows[-1]["ts"]
    cutoff = now - window_min * 60 if window_min > 0 else rows[0]["ts"] - 1
    win = [r for r in rows if r["ts"] >= cutoff]
    if len(win) < 2:
        win = [r for r in rows if r["percent"] < rows[-1]["percent"]] or rows
        win = (win + rows[-1:])
    if len(win) < 2:
        return None, "none", 0.0, 0.0

    first, last = win[0], win[-1]
    d_pct = last["percent"] - first["percent"]
    d_min = (last["ts"] - first["ts"]) / 60.0
    if d_min <= 0:
        return None, "none", 0.0, 0.0
    if d_pct <= 0:
        return None, "idle", d_min, 0.0

    rate = d_pct / (d_min / 60.0)
    if d_min >= 120 and d_pct >= 3:
        conf = "high"
    elif d_min >= 30 and d_pct >= 1:
        conf = "medium"
    else:
        conf = "low"
    return rate, conf, d_min, d_pct


def fmt_dur(hours):
    if hours is None:
        return "—"
    if hours < 0:
        return "已过期"
    if hours < 1:
        return f"{hours * 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def human_time(epoch):
    if not epoch:
        return "—"
    return dt.datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")


def build_report(window_min, warn_threshold):
    snaps, n_files, n_rec = load_snapshots()
    if not snaps:
        return {"models": [], "error": "未找到配额快照（是否还没用 WorkBuddy 跑过对话？）",
                "files": n_files, "records": n_rec}

    now = dt.datetime.now().timestamp()
    out = []
    for model, rows in snaps.items():
        last = rows[-1]
        pct = last["percent"]
        reset = last["reset_at"]
        h_to_reset = (reset - now) / 3600.0 if reset else None

        # 最后一条快照的「重置时刻」已经过去 → 它属于上一周期，本周期还没用过它。
        # 此时 percent 是旧周期的残值，再报告「距重置 已过期」毫无意义。
        stale = bool(reset and now > reset + RESET_GRACE)

        # 本周期起点 = 下个重置时刻 - 一个周期。只统计这段时间内的快照，
        # 让 tokens / 积分 与屏幕上的百分比属于同一段账。
        cycle_start = (reset - PERIOD_H * 3600.0) if reset else None
        cyc = [r for r in rows if cycle_start is None or r["ts"] >= cycle_start - 1.0]
        if not cyc:
            cyc = rows[-1:]

        rate, conf, span_min, d_pct = burn_rate(cyc, window_min)
        eta = None
        if rate and rate > 0 and pct < 100:
            eta = (100.0 - pct) / rate

        # 判定：百分比是整数，短样本的速率不可信，低置信度只提示不告警
        if pct >= 95:
            level, verdict = "critical", "已到上限，随时会被拒"
        elif conf in ("low", "none") and pct >= 80:
            level, verdict = "critical", f"已用 {pct:g}%，样本太短无法预测，按最坏打算"
        elif conf in ("low", "none"):
            if d_pct > 0:
                level, verdict = "idle", f"在消耗，但样本仅 {span_min:.0f} 分钟，暂不预测"
            elif conf == "idle":
                level, verdict = "ok", "近期无新增消耗"
            else:
                level, verdict = "idle", "样本不足，无法预测"
            eta = None
        elif eta is not None and h_to_reset is not None and eta < h_to_reset:
            level, verdict = "critical", "重置前就会耗尽"
        elif eta is not None and eta < warn_threshold:
            level, verdict = "warn", f"{fmt_dur(eta)}内触限"
        else:
            level, verdict = "ok", "按当前节奏够用"

        out.append({
            "model": model,
            "percent_used": round(pct, 2),
            "percent_left": round(max(0.0, 100.0 - pct), 2),
            "reset_at": reset,
            "reset_local": human_time(reset),
            "hours_to_reset": round(h_to_reset, 2) if h_to_reset is not None else None,
            "rate_pct_per_hour": round(rate, 2) if rate else None,
            "rate_confidence": conf,
            "sample_span_min": round(span_min, 1),
            "eta_hours_to_limit": round(eta, 2) if eta is not None else None,
            "shared_models": last["shared"],
            "requests": len(rows),
            "credit_total": round(sum(r["credit"] for r in rows), 2),
            "tokens_total": sum(r["tokens"] for r in rows),
            "stale": stale,
            "cycle_start": cycle_start,
            "cycle_start_local": human_time(cycle_start) if cycle_start else None,
            "cycle_hours": PERIOD_H,
            "requests_cycle": len(cyc),
            "credit_cycle": round(sum(r["credit"] for r in cyc), 2),
            "tokens_cycle": sum(r["tokens"] for r in cyc),
            "level": level,
            "verdict": verdict,
            "first_seen": human_time(rows[0]["ts"]),
            "last_seen": human_time(last["ts"]),
            # 数值版，供 latest_row 排序。last_seen 是 "MM-DD HH:MM"，
            # 跨年时 12-31 会大于 01-01 —— 字符串比不出真正的先后
            "last_seen_ts": last["ts"],
        })

    order = {"critical": 0, "warn": 1, "ok": 2, "idle": 3}
    out.sort(key=lambda r: (order[r["level"]], -r["percent_used"]))
    live = [r for r in out if not r["stale"]]
    risk = any(r["level"] == "critical" for r in live)
    return {"models": out, "files": n_files, "records": n_rec, "has_risk": risk,
            "window_min": window_min, "generated_at": human_time(now),
            "active_models": len(live), "stale_models": len(out) - len(live),
            "cycle_hours": PERIOD_H}


ICON = {"critical": "[!!]", "warn": "[! ]", "ok": "[ok]", "idle": "[--]"}
CONF_CN = {"high": "高", "medium": "中", "low": "低(样本短)", "idle": "无消耗", "none": "—"}


def print_table(rep):
    if rep.get("error"):
        print("  " + rep["error"])
        return
    print()
    print(f"  WorkBuddy 模型配额 · {rep['generated_at']} · "
          f"增速窗口 {rep['window_min']} 分钟 · 数据 {rep['files']} 个会话文件 / {rep['records']} 条快照")
    print()
    head = (f"  {'':4}{'模型':24}{'已用':>6}{'剩余':>6}{'重置时间':>13}"
            f"{'距重置':>10}{'增速/h':>9}{'预计触限':>11}{'可信度':>12}")
    print(head)
    print("  " + "-" * (len(head) + 8))
    live = [r for r in rep["models"] if not r.get("stale")]
    stale_n = len(rep["models"]) - len(live)
    for r in live:
        pct = f"{r['percent_used']:g}%"
        left = f"{r['percent_left']:g}%"
        rate = f"{r['rate_pct_per_hour']:g}%" if r["rate_pct_per_hour"] else "—"
        eta = fmt_dur(r["eta_hours_to_limit"])
        to_reset = fmt_dur(r["hours_to_reset"])
        conf = CONF_CN.get(r["rate_confidence"], "—")
        print(f"  {ICON[r['level']]}{r['model'][:24]:24}{pct:>6}{left:>6}"
              f"{r['reset_local']:>13}{to_reset:>10}{rate:>9}{eta:>11}{conf:>12}")
        print(f"         └ {r['verdict']}"
              + (f" · 与 {', '.join(r['shared_models'])} 共用配额" if r["shared_models"] else "")
              + f" · 本周期 {r.get('requests_cycle', r['requests'])} 次"
              + f" · {r.get('tokens_cycle', r['tokens_total']):,} tokens"
              + f" · {r.get('credit_cycle', r['credit_total']):g} 积分")
    if not live:
        print("  本周期还没有使用记录。")
    if stale_n:
        print(f"  另有 {stale_n} 个模型停在上一周期（重置后未使用），不计入上表。")
    print()
    if rep["has_risk"]:
        print("  [!!] 有模型存在触限风险。建议：先把长任务的关键结论落盘，再切到其他模型继续，")
        print("       或把大批量工作拆成小步，避免在额度耗尽时丢失上下文。")
    else:
        print("  [ok] 按当前节奏，没有模型会在配额重置前耗尽。")
    print("  提示：百分比粒度为整数，短时波动会放大误差；增速随闲置自动回落。")
    print()


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 模型配额胶囊")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--window", type=int, default=60, help="增速统计窗口（分钟，默认 60）")
    ap.add_argument("--warn", type=float, default=3.0, help="预计触限小于该小时数即告警（默认 3）")
    ap.add_argument("--warn-only", action="store_true", help="无风险时不输出")
    args = ap.parse_args()

    rep = build_report(args.window, args.warn)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif not (args.warn_only and not rep.get("has_risk")):
        print_table(rep)
    return 1 if rep.get("has_risk") else 0


if __name__ == "__main__":
    sys.exit(main())
