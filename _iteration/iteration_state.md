# 迭代状态（桌面版 · 第五周期）

轮次：5 / 5（D6–D10 完成 · 迭代结束）
迭代对象：`D:\AI_work\WorkBuddy_work\2026-11-05-22-37-57\workbuddy-quota-pill`
基线备份路径：`_iteration/baseline_20261120_2302.zip`（git archive，`354c056`，260 KB）
起始版本：v0.2.1（本周期只改文档与说明，不动运行行为）
上一周期：`迭代报告_D1..D5.md` + `总结报告_D1-D5.md`（收敛于 v0.2.1）
本周期报告：`迭代报告_D6..D10.md`；总结见 `总结报告_D6-D10.md`
技能版：R11–R15 已结束，见 `workbuddy-quota-pill-skills/_iteration/`

## 已修 Bug 清单（本周期）

- [B8] P2　`README.en.md` 有两段重复的排查条目（5–8 出现两次、编号从 5 重来）。
- [B9] P2　`README.en.md` 仍写 `%LOCALAPPDATA%\quota-watch\tray.log`——运行时已改名
  `quota-pill`，照它去找日志会扑空。
- [B10] P2　`docs/design.md` 说「`shared_models` 目前观测为空」，实测 5948 条快照里 14 条
  带该字段（`hy4-flash` 与 `hy4free` 共用）。
- [B11] P3　`docs/design.md` 的 TODO 写死「3112 行」，实际 3250 行。
- [B12] P2　`使用说明.md` 的「包内文件」表缺 `forecast.py`（包里已含，清单没跟）。
- [B13] P2　`--max-rows 0` 让卡片返回空行、`-1` 被当成切片少画一行（高度按 1 行算，对不上）。
- [B14] P2　`--refresh 0/负数` 让刷新判据恒真，每个轮询周期重扫日志（0.4–0.7 秒）。
- [B15] P2　`README.md` / `使用说明.md` 的覆盖率写「deepseek 约一半、hy4 不足一成」，
  实测 67% 与 10.7%。

## 待修 Bug 清单

无 P0 / P1。

## 观察项与不做事项

- **观察 [O3]**：`quota_watch.py` 的 `elif conf == "idle"` 分支不可达（`idle` 不在
  `("low","none")` 里）。删它要两仓同改，价值低，**不做**；design.md 已如实记录。
- 历史观察项（第四周期）见文末。

## 回归用例集（本周期新增）

| 编号 | 预期 | 执行方式 | 当前 |
|---|---|---|---|
| T11 | 英文 README 无重复排查段 | `grep -c "The pill doesn't appear"` = 1 | 通过 |
| T12 | 英文 README 不再出现 `quota-watch` 路径 | `grep -c quota-watch README.en.md` = 0 | 通过 |
| T13 | design.md 不再宣称 `shared_models` 恒空 | 文本比对 | 通过 |
| T14 | 说明书包内文件与 `FILES` 一一对应 | 清单比对 | 通过 |
| T15 | `--max-rows` 边界夹取（0/负/非数字） | `tests/test_tray.py` | 通过 |
| T16 | `--refresh` 下限 5 秒 | 同上 | 通过 |
| T17 | 文档覆盖率与实测一致 | 统计脚本 + grep | 通过 |

T1–T10 见文末历史表，本轮全量复跑通过。

## 上轮改动结果（D6）

- [C27] 保留　`README.en.md` 去重复段（B8）
- [C28] 保留　`README.en.md` 日志路径改 `quota-pill`（B9）
- [C29] 保留　`design.md` 的 `shared_models` 结论按实测改写 + TODO 去掉死数字（B10/B11）
- [C30] 保留　`使用说明.md` 包内文件表补 `forecast.py`（B12）
- [C31] 保留　新增 `clamp_rows()` + 5 条用例（B13）
- [C32] 保留　新增 `clamp_refresh()`（下限 5 秒）+ 4 条用例 + help（B14）
- [C33] 保留　README / 使用说明的覆盖率改「约三分之二」「约一成」（B15）

## 连续无实质改进轮数

0

## 收敛判定

**迭代结束**。原因：达到最大轮次（5/5）；待修清单为空；core 69/69、tray 193/193 全通过。
剩余事项：v0.2.1-beta 的附件是改前打的包（说明书已更新，需发布者决定是否重打）；
`quota_watch.py` 的不可达分支按 O3 不做。

## 中断标记

无

---

# 历史：第四周期（D1–D5 · 已结束）

轮次：5 / 5（D1–D5，**已结束**）
迭代对象：`D:\AI_work\WorkBuddy_work\2026-11-05-22-37-57\workbuddy-quota-pill`
基线备份路径：`_iteration/baseline_20261115_1946`（12 文件 / 416 KB）
起始版本：0.2.0 → 结束版本 **0.2.1**
本周期报告：`迭代报告_D1..D5.md`；总结见 `总结报告_D1-D5.md`
共用文件影响：`forecast.py` 两处改动已同步到技能仓，技能版随之 0.5.1 → 0.5.2

## 已修 Bug 清单

- [B1] P3　`--window` help 仍用内部单位「格」。
- [B2] P2　README 中英与 design.md 硬编码测试项数（168/172/56），已过期且会继续漂。
- [B3] P2　`使用说明.md` / `README.md` 把「近 15 分钟无新增」说成报告里的文字，实际只在卡片。
- [B4] P2　`--window N` 只改读数不改文案，副标题恒说「近 15 分钟」。
- [B5] P2　详情页标签写死「近 15 分钟」，与 45 分钟窗口的数值自相矛盾。
- [B6] P3　文件头与 `row_texts` docstring 停留在 0.4.x（「所有数字来自 build_report」、
  「+9 格（36 格/小时）」）。
- [B7] P3　`--window 0` 被 `or` 当成假值回落；`--window -5` 直接印「近 -5 分钟」。

## 待修 Bug 清单

无。

## 观察项与不做事项

- **观察**：`quota_watch.py`（数据层）的 `level`/`verdict` 仍是 0.4.x 口径，展示层已用
  `forecast.attach()` 覆盖。数据层要保持与技能仓逐字节同源，**不做**。
- **观察**：`--print` 没有退出码语义（不像技能版 `quota_report` 会因风险返回 1）。
  桌面版是常驻 GUI，没有脚本消费方，**不做**。
- **不做**：给 `--window` 加上限（如 ≤240）。目前任意正整数都能算，没有已知坏效果。

## 回归用例集

| 编号 | 预期 | 执行方式 | 当前 |
|---|---|---|---|
| T1 | 数据层 56/56 | `tests/test_core.py` | 通过 |
| T2 | 展示层 173/173 | `tests/test_tray.py` | 通过 |
| T3 | 帮助文本无「格」 | `--help` 扫描 | 通过 |
| T4 | 文档无硬编码测试项数 | grep 四个 md | 通过 |
| T5 | 卡片文案窗口随 `--window` | `--print --window 45` | 通过 |
| T6 | `decide()` 结果带 `window_min` | 单测 | 通过 |
| T7 | 详情页标签随窗口（离屏渲染） | `preview.py --report --window 45` | 通过 |
| T8 | 窗口 0 / 负数夹到 1 | 单测 + `--print` | 通过 |
| T9 | 两侧同源 | `sync_check.py` | 通过 |
| T10 | 技能版全量未回归 | 42/42 + 37/37 | 通过 |

## 上轮改动结果

- [C1] 保留　`--window` help（B1）
- [C2] 保留　删硬编码项数（B2）
- [C3] 保留　「近 15 分钟无新增」出处写准（B3）
- [C4] 保留　`decide()` 带出 `window_min` + `phrase()` 用它（B4）
- [C5] 保留　新增 4 条用例（B4）
- [C6] 保留　详情页标签随窗口（B5）
- [C7] 保留　注释与 docstring 更新（B6）
- [C8] 保留　离屏渲染验证（B5）
- [C9] 保留　窗口夹取 + 显式 None 判断（B7）
- [C10] 保留　新增 3 条用例（B7）
- [C11] 保留　版本 v0.2.1
- [C12] 保留　技能版回流 0.5.2 + 重打包

## 连续无实质改进轮数

1（D5 只有版本号变更；因达到轮次上限与待修清空，判定结束）

## 收敛判定

**迭代结束**。D1–D5 五轮，B1–B7 全修，T1–T10 全通过。

## 中断标记

无
