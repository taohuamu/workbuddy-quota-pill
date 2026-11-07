"""造一份假的 WORKBUDDY_HOME，用来预演各种界面状态（不动真实日志）。

用法:
    python tools/fixtures.py                         # 3 个模型，正常/注意/危险
    python tools/fixtures.py --models 6 --out X      # 6 个模型，测多行折叠
    python tools/fixtures.py --empty                 # 空数据（测空态）

生成后可这样预览：
    python tools/preview.py out.bmp --home <生成目录>
"""
import argparse
import json
import os
import random
import shutil
import tempfile
import time

# (模型, 当前百分比, 窗口内涨幅比例, 距重置小时数)
# 这组数值刚好能凑出四档 level：critical / warn / ok / idle，方便肉眼比对配色
PRESETS = [
    ("deepseek-v4.1-flash", 92, 0.26, 8.5),   # critical：重置前就会耗尽
    ("glm-5.3-flash", 55, 0.29, 1.0),         # warn：几小时内触限
    ("gpt-5.1", 73, 0.20, 1.0),               # warn
    ("deepseek-v4-pro", 18, 0.04, 2.0),       # ok：重置前用不完
    ("claude-sonnet-4-5", 47, 0.10, 2.0),     # ok
    ("hy4-free", 4, 0.00, 12.0),              # 几乎没动
]


def build(out, n=3, empty=False, minutes=55):
    if os.path.isdir(out):
        shutil.rmtree(out)
    proj = os.path.join(out, "projects", "fixture")
    os.makedirs(proj, exist_ok=True)
    if empty:
        return out

    now = time.time()
    lines = []
    for name, target, frac, reset_h in PRESETS[:n]:
        reset_at = now + reset_h * 3600
        start = max(0, target - round(target * frac))
        steps = 24
        for i in range(steps):
            pct = round(start + (target - start) * (i + 1) / steps)
            ts = now - (minutes - i * minutes / steps) * 60
            lines.append({
                "type": "assistant",
                "timestamp": int(ts * 1000),
                "providerData": {
                    "model": name,
                    "rawUsage": {
                        "quota": {
                            "model": name,
                            "percent": pct,
                            "reset_at": int(reset_at * 1000),
                            "shared_models": [],
                        },
                        "credit": round(random.uniform(0.3, 1.6), 2),
                        "total_tokens": random.randint(30000, 120000),
                    },
                },
            })
    lines.sort(key=lambda r: r["timestamp"])
    path = os.path.join(proj, "fixture-session.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for rec in lines:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "quota-watch-fixture"))
    ap.add_argument("--models", type=int, default=3)
    ap.add_argument("--empty", action="store_true")
    args = ap.parse_args()
    p = build(args.out, args.models, args.empty)
    print("fixture home:", args.out)
    print("jsonl:", p if not args.empty else "(空)")
    print("预览: python tools/preview.py out.bmp --home", args.out)


if __name__ == "__main__":
    main()
