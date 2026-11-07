# WorkBuddy 配额小窗

![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
[![release](https://img.shields.io/github/v/release/taohuamu/workbuddy-quota-pill?include_prereleases&label=release)](https://github.com/taohuamu/workbuddy-quota-pill/releases)

常驻屏幕右下角的一枚胶囊，显示**最近一次对话用的模型**和它这个周期的用量。碰一下展开，
看清哪个模型快被打满了。

> 只读本机日志 —— 不联网、不上传、不需要任何账号或密钥。

**[下载最新版](https://github.com/taohuamu/workbuddy-quota-pill/releases)** ·
[使用说明](使用说明.md) ·
[设计档案](docs/design.md)

---

## 它解决什么问题

WorkBuddy 的界面不显示「这个模型今天用了百分之多少」。这个数字其实随每一轮响应下发，
藏在会话日志的 `providerData.rawUsage.quota` 字段里 —— 但没有任何界面去读它。

于是常见的情况是：写到一半突然被拒，或者凭感觉换模型。这个小窗把那个数字搬到桌面上，
并且在**打满之前**告诉你还有多久。

和多数同类工具的区别：它们统计「已经花了多少 token / 多少积分」，这里算的是
「会不会在配额重置前耗尽」。两个问题的答案完全不同 —— 用了 40% 不等于安全，
如果重置还剩 20 小时而你在 3 小时里花掉了 40%，那才是真的危险。

## 三个形态

### ① 胶囊

WorkBuddy 打开后出现在右下角。显示最近一次对话用的模型，和它本周期的用量 ——
一眼就知道当前这个模型还剩多少。

<p align="center"><img src="docs/images/pill.png" width="236" alt="胶囊态"></p>

### ② 小卡片

鼠标碰一下胶囊，180ms 后展开。最多列出 3 个模型，每个一条进度条 + 一句判决语
（「预计 10.9 小时后触限」这类），比只看百分比更能判断要不要收手。

<p align="center"><img src="docs/images/card.png" width="378" alt="展开态"></p>

### ③ 完整报告窗

点卡片上任意一行（或右上角「详情」）打开。本周期用过的全部模型，
含重置时间、增速、触限预测，以及周期内的 token 与积分消耗。

<p align="center"><img src="docs/images/report.png" width="720" alt="完整报告窗"></p>

不开 WorkBuddy 的时候它不出现；WorkBuddy 退出后 10 秒，小窗自己结束，不留常驻的
Python 进程。为了不错过启动时机，附带一个看门狗脚本（约 5MB 内存，CPU 占用约 0.08%），
WorkBuddy 启动时负责把小窗拉起来。

## 安装

**方式一：下载压缩包**（推荐）

从 [Releases](https://github.com/taohuamu/workbuddy-quota-pill/releases) 下载
`quota-watch-beta-*.zip`，解压到任意目录（**路径别带空格和中文**），双击 `tray.bat`。

**方式二：克隆仓库**

```bash
git clone https://github.com/taohuamu/workbuddy-quota-pill.git
cd workbuddy-quota-pill
```

同样双击 `tray.bat`。

**前置要求**：Windows 10/11 + Python 3.8 以上。Python 官网安装包默认就带 tkinter，
不用额外装。如果双击后什么都没出现，在解压目录打开命令提示符运行：

```bash
python quota_tray.py --which
```

列表里要有以 `tk` 开头的一行，全是 `--` 说明当前 Python 缺 tkinter，需要重装 Python
并勾选 "tcl/tk and IDLE"。

**开机自启（可选）**：右键小窗 → 勾选「开机自动待命」。

## 用法

| 操作 | 效果 |
|---|---|
| 鼠标碰一下胶囊 | 展开成卡片 |
| 移开鼠标 | 2.2 秒后自动收回 |
| 拖动 | 横向纵向都能拖 |
| 点某一行 / 「详情」 | 打开完整报告 |
| 右键 | 刷新 / 完整报告 / 保存布局 / 恢复默认位置 / 各开关 |

拖到满意的地方后要**右键「保存当前布局」**才记住 —— 拖动本身不写盘，
这样误拖一下不会把布局弄乱。

## 配额口径

- 周期按 **24 小时**算：`cycle_start = reset_at - 24h`，只看这个窗口内的记录
- 已过重置时间的旧模型不再显示（避免出现「重置后一次没用却显示 11%」这种自相矛盾的读数）
- 多个模型共用同一份额度时会标注「与 N 个模型共用」

## 已知限制

- **GLM 等部分模型不显示用量**：这些模型在本地日志里没有 `quota` 字段，读不到百分比。
  不确定是服务端不下发还是客户端没记录 —— 总之取不到数据，不是漏读。
- **卡片可能偏窄**：为了不压住输入框，卡片被限制在输入框右侧的空档里，
  长模型名会截断成 `deepseek-...`。想要更宽可以右键取消「展开时不压输入框」，
  代价是会盖住输入框一角。
- **刚重置、还没用过的模型不显示**：这是有意的，显示一个 0% 的空模型没有意义。
- **换过缩放比例或显示器后位置可能偏**：右键「重新校准位置」重量一次。

## 数据来源

```
%USERPROFILE%\.workbuddy\projects\<project-key>\<session-id>.jsonl
```

每行一轮请求，取 `providerData.rawUsage.quota`。国际版路径是 `~/.workbuddy-ai/projects/...`，
可用环境变量 `WORKBUDDY_HOME` 覆盖根目录。

这个工具只读文件，不写 WorkBuddy 的任何数据，也没有任何网络请求 ——
所有数据都留在你自己的机器上。

## 开发

```bash
python tests/test_tray.py     # 展示层纯逻辑，157 项
python tests/test_core.py     # 数据层，41 项
python tools/package.py       # 打内测包到 ../dist/
python tools/preview.py out.bmp --pill   # 离屏渲染一张图
```

图形部分没有自动断言 —— 分层窗口（`-transparentcolor`）的行为很难在无头环境复现，
相关判断靠人工看 `tools/preview.py` 抓出来的图。

设计取舍、踩过的坑、坐标系约定都在 [`docs/design.md`](docs/design.md)。

## 声明

本项目是第三方工具，**与腾讯 / WorkBuddy 官方无关**，未获得任何形式的技术支持或授权。
它只读取本机已经存在的日志文件。

## License

[MIT](LICENSE)
