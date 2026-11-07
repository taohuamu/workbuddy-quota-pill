#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota-watch 回归测试（无第三方依赖，直接 python tests/test_core.py）

不读取真实会话日志：所有 build_report 用例都在临时目录里伪造 jsonl，
只有纯函数用例直接构造 dict。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quota_watch as qw  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def line(ts_ms, percent, reset_at_s, model="m-test", credit=0.1, tokens=1000, shared=None):
    rec = {
        "type": "function_call",
        "timestamp": ts_ms,
        "providerData": {
            "model": model,
            "rawUsage": {
                "credit": credit,
                "total_tokens": tokens,
                "quota": {
                    "model": model,
                    "percent": percent,
                    "reset_at": str(reset_at_s),
                    "shared_models": shared or [],
                },
            },
        },
    }
    return json.dumps(rec, ensure_ascii=False)


def write_case(proj_dir, rows):
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "sess.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(r + "\n")


def test_norm_reset():
    check("秒级时间戳原样保留", qw.norm_reset("1793975887") == 1793975887.0)
    check("毫秒级时间戳被归一", qw.norm_reset(1793975887000) == 1793975887.0)
    check("非法值返回 None", qw.norm_reset("abc") is None and qw.norm_reset(None) is None)


def test_fmt_dur():
    check("None 显示破折号", qw.fmt_dur(None) == "—")
    check("负数显示已过期", qw.fmt_dur(-1) == "已过期")
    check("半小时按分钟", qw.fmt_dur(0.5) == "30 分钟")
    check("两小时按小时", qw.fmt_dur(2.0) == "2.0 小时")
    check("三天按天", qw.fmt_dur(72) == "3.0 天")


def rows_at(start_s, span_min, pct_from, pct_to, steps=5):
    """在 start_s 起的 span_min 内均匀分布 steps 条快照，百分比线性上升。"""
    out = []
    for i in range(steps):
        t = start_s + span_min * 60 * i / (steps - 1)
        pct = pct_from + (pct_to - pct_from) * i / (steps - 1)
        out.append({"ts": t, "percent": float(pct)})
    return out


def test_burn_rate():
    rows = rows_at(0, 240, 10, 30)
    rate, conf, span, d = qw.burn_rate(rows, 240)
    check("高置信：4 小时涨 20%", conf == "high" and abs(rate - 5.0) < 0.01, f"{rate} {conf}")
    check("跨度回传正确", abs(span - 240) < 0.5, span)

    rate, conf, span, d = qw.burn_rate(rows, 60)
    check("窗口收窄后只取末段", conf == "medium" and abs(span - 60) < 0.5, f"{span} {conf}")

    rows = rows_at(0, 60, 10, 20)
    rate, conf, span, d = qw.burn_rate(rows, 120)
    check("中置信：1 小时涨 10%", conf == "medium" and abs(rate - 10.0) < 0.01, f"{rate} {conf}")

    rows = rows_at(0, 10, 1, 2)
    rate, conf, span, d = qw.burn_rate(rows, 60)
    check("低置信：10 分钟涨 1%", conf == "low", conf)

    rows = rows_at(0, 60, 12, 12)
    rate, conf, span, d = qw.burn_rate(rows, 60)
    check("无增长判为 idle", rate is None and conf == "idle", conf)

    rows = [{"ts": 0, "percent": 5.0}]
    rate, conf, span, d = qw.burn_rate(rows, 60)
    check("单条样本无速率", rate is None and conf == "none", conf)


def test_cycle_and_stale():
    """周期口径：重置后没再用过的模型标 stale；tokens/积分只算本周期。"""
    tmp = tempfile.mkdtemp(prefix="qw-cycle-")
    origin = qw.PROJECTS
    try:
        now_s = time.time()
        now_ms = int(now_s * 1000)
        d = os.path.join(tmp, "cycle")

        # m-old：最后一条快照的重置时刻已经过去（上一周期的残值）
        # m-live：跨了两个周期，只有最近那批属于本周期
        rows = [
            line(now_ms - 3 * 3600 * 1000, 88, now_s - 3600, model="m-old",
                 credit=1.0, tokens=5000),
            line(now_ms - 30 * 3600 * 1000, 50, now_s + 14 * 3600, model="m-live",
                 credit=2.0, tokens=70000),
            line(now_ms - 25 * 3600 * 1000, 51, now_s + 14 * 3600, model="m-live",
                 credit=2.0, tokens=70000),
            line(now_ms - 10 * 60 * 1000, 52, now_s + 14 * 3600, model="m-live",
                 credit=0.5, tokens=3000),
        ]
        write_case(d, rows)
        qw.PROJECTS = tmp
        rep = qw.build_report(60, 3.0)

        by = {r["model"]: r for r in rep["models"]}
        check("过期快照标记 stale", by["m-old"]["stale"] is True, by["m-old"]["stale"])
        check("本周期在用的不标 stale", by["m-live"]["stale"] is False, by["m-live"]["stale"])
        check("有效模型计数只数未过期的", rep["active_models"] == 1, rep["active_models"])
        check("过期模型计入 stale 计数", rep["stale_models"] == 1, rep["stale_models"])
        check("过期模型不参与风险判定", rep["has_risk"] is False, rep["has_risk"])

        live = by["m-live"]
        check("本周期 tokens 只算周期内快照",
              live["tokens_cycle"] == 3000, live["tokens_cycle"])
        check("历史累计仍然完整",
              live["tokens_total"] == 143000, live["tokens_total"])
        check("本周期积分只算周期内快照",
              abs(live["credit_cycle"] - 0.5) < 1e-6, live["credit_cycle"])
        check("本周期样本数只算周期内",
              live["requests_cycle"] == 1, live["requests_cycle"])
        check("周期起点 = 重置时刻 - 一个周期",
              abs((live["cycle_start"] or 0) - (now_s + 14 * 3600 - 24 * 3600)) < 2,
              live["cycle_start"])
    finally:
        qw.PROJECTS = origin


def test_build_report_cases():
    tmp = tempfile.mkdtemp(prefix="qw-test-")
    origin = qw.PROJECTS
    try:
        now_s = time.time()
        now_ms = int(now_s * 1000)

        # 用例 1：慢速消耗，重置前用不完 -> ok
        d1 = os.path.join(tmp, "safe")
        write_case(d1, [
            line(now_ms - 180 * 60 * 1000, 10, now_s + 6 * 3600),
            line(now_ms - 120 * 60 * 1000, 11, now_s + 6 * 3600),
            line(now_ms, 12, now_s + 6 * 3600),
        ])

        # 用例 2：快速消耗，4 小时就见底但 6 小时才重置 -> critical
        d2 = os.path.join(tmp, "burn")
        write_case(d2, [
            line(now_ms - 240 * 60 * 1000, 60, now_s + 6 * 3600, model="m-burn"),
            line(now_ms - 120 * 60 * 1000, 70, now_s + 6 * 3600, model="m-burn"),
            line(now_ms, 80, now_s + 6 * 3600, model="m-burn"),
        ])

        # 用例 3：样本仅 10 分钟 -> 低置信，不预测，idle
        d3 = os.path.join(tmp, "thin")
        write_case(d3, [
            line(now_ms - 10 * 60 * 1000, 1, now_s + 6 * 3600, model="m-thin"),
            line(now_ms, 2, now_s + 6 * 3600, model="m-thin"),
        ])

        # 用例 4：重置就在 2 小时后、当前 50%，但速率会撑到 2.5 小时 -> warn
        d4 = os.path.join(tmp, "warn")
        write_case(d4, [
            line(now_ms - 60 * 60 * 1000, 30, now_s + 2 * 3600, model="m-warn"),
            line(now_ms, 50, now_s + 2 * 3600, model="m-warn"),
        ])

        def by_model(rep, m):
            for r in rep["models"]:
                if r["model"] == m:
                    return r
            raise AssertionError(f"缺少模型 {m}")

        qw.PROJECTS = tmp
        rep = qw.build_report(60, 3.0)
        check("四个用例都被采到", len(rep["models"]) == 4, [r["model"] for r in rep["models"]])

        r1 = by_model(rep, "m-test")
        check("慢速消耗判为 ok", r1["level"] == "ok", f"{r1['level']} {r1['verdict']}")
        check("慢速消耗的验语正确", r1["verdict"] == "按当前节奏够用", r1["verdict"])
        check("累计积分被汇总", abs(r1["credit_total"] - 0.3) < 1e-6, r1["credit_total"])

        r2 = by_model(rep, "m-burn")
        check("重置前耗尽判为 critical", r2["level"] == "critical", f"{r2['level']} {r2['verdict']}")
        check("critical 验语指向重置前", r2["verdict"] == "重置前就会耗尽", r2["verdict"])
        check("预计触限约 4 小时", abs(r2["eta_hours_to_limit"] - 4.0) < 0.2, r2["eta_hours_to_limit"])

        r3 = by_model(rep, "m-thin")
        check("样本过短不预测", r3["level"] == "idle" and r3["eta_hours_to_limit"] is None,
              f"{r3['level']} {r3['eta_hours_to_limit']}")
        check("置信度标为 low", r3["rate_confidence"] == "low", r3["rate_confidence"])

        r4 = by_model(rep, "m-warn")
        check("临近重置但撑得住判为 warn", r4["level"] == "warn", f"{r4['level']} {r4['verdict']}")

        check("汇总标记存在风险", rep["has_risk"] is True)
        check("critical 排在首位", rep["models"][0]["level"] == "critical", rep["models"][0]["model"])
        levels = [r["level"] for r in rep["models"]]
        check("排序：warn 在 ok 之前", levels.index("warn") < levels.index("ok"), levels)
        check("排序：idle 排最后", levels[-1] == "idle", levels)
    finally:
        qw.PROJECTS = origin
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_dir():
    tmp = tempfile.mkdtemp(prefix="qw-empty-")
    origin = qw.PROJECTS
    try:
        qw.PROJECTS = tmp
        rep = qw.build_report(60, 3.0)
        check("空目录返回 error 而不是崩溃", "error" in rep, rep.get("error"))
        check("空目录不算风险", rep.get("has_risk") is not True)
    finally:
        qw.PROJECTS = origin
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("quota-watch 回归测试")
    test_norm_reset()
    test_fmt_dur()
    test_burn_rate()
    test_cycle_and_stale()
    test_build_report_cases()
    test_empty_dir()
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
