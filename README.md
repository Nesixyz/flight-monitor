# ✈️ 上海 → 巴黎 机票价格监控

> 基于飞猪官方 API（flyai-cli），自动监控上海 → 巴黎机票，命中低价即推送微信通知 + 在线看板展示。

**当前监控示例参数（可修改）**：
- 出发日期：2026-09-16 ~ 2026-10-02（共 17 天，全量查询）
- 价格阈值：公务舱 ≤¥13000 / 经济舱 ≤¥5000（含税）
- 中转次数：≤1（直飞优先，接受 1 次中转）
- 行李要求：飞猪返回结果自动含行李额度，留学生专享票价方案均接受
- 推荐航班（推送和看板顶部展示）：公务 ≤¥10000 / 经济 ≤¥5000，日期 09-23 ~ 10-01，总时长最短

---

## 🌟 功能特性

| 特性 | 说明 |
|---|---|
| 🔍 **全量日期监控** | 每次运行完整查询 [date_start, date_end] 区间内所有日期（公务+经济双舱位） |
| 🏷️ **变化对比** | 与上次结果对比，标注 🆕新增 / ⬇降价 / ⬆涨价 / —不变 / ❌消失 |
| 📱 **Server酱微信推送** | 每次运行有内容即推，推送额度按阶段动态匹配（2~4 条/天），无需担心刷屏 |
| ⭐ **智能推荐** | 按更严格价格+日期窗口自动选出公务/经济各 1 条总时长最短的航班放在顶部 |
| 📊 **在线看板** | GitHub Pages 展示完整结果（公务/经济分类表 + 推荐卡片 + 风控/消失提示） |
| 💰 **飞猪额度友好** | 三阶段动态频率（抓低价3次/天、稳观察2次/天、冲刺期4次/天），每天最多 ~68 次调用 |
| 🧭 **跨时区时长修正** | 基于 `segment.duration` 求和（飞猪本地时间已校正），避免上海/巴黎 6 小时时差导致总时长算短 |
| 🔒 **敏感信息隔离** | key 走环境变量/Secrets，`config.json` 本地用不提交 |
| ⚡ **手动触发旁路** | 设 `MANUAL_RUN=1` 跳过时段检查立即执行，方便调试 |

---

## 🏗️ 架构概览

```
          ┌──────────────────────────┐
          │   GitHub Actions cron    │  (北京时间 0/6/12/18)
          │   (workflow_dispatch)    │  或手动触发
          └────────────┬─────────────┘
                       │
          ┌────────────▼─────────────┐
          │     monitor.py 主脚本    │
          │  ┌─────────────────────┐ │
          │  │ 阶段判断 (should_run)│ │ 0-跳过 | 1-执行
          │  └──────────┬──────────┘ │
          │  ┌──────────▼──────────┐ │
          │  │ flyai-cli 全量查询   │ │ → 原始机票列表
          │  └──────────┬──────────┘ │
          │  ┌──────────▼──────────┐ │
          │  │ parse_flight 结构化 │ │ → 舱位/起降/路线/时长(校正)
          │  └──────────┬──────────┘ │
          │  ┌──────────▼──────────┐ │
          │  │ 价格/中转筛选       │ │ → 命中航班列表
          │  └──────────┬──────────┘ │
          │  ┌──────────▼──────────┐ │
          │  │ diff_flights 变化对比│ │ + 推荐航班
          │  └──────────┬──────────┘ │
          └────────────┬─┬───────────┘
                       │ │
        ┌──────────────┘ └──────────────┐
┌───────▼────────┐            ┌─────────▼──────────┐
│ Server酱 推送  │            │ dashboard.html 看板 │
│ (微信消息)     │            │ (GitHub Pages 部署) │
└────────────────┘            └────────────────────┘
```

---

## 🚀 快速开始（本地）

### 环境要求

- **Python** ≥ 3.9（macOS 自带 `/usr/bin/python3` 即可，仅用标准库）
- **Node.js** ≥ 20（安装飞猪 CLI）
- 飞猪开放平台 API Key（登录首赠 5000 次永久调用额度，申请入口见 flyai-cli 官方文档）
- Server酱 SendKey（[sct.ftqq.com](https://sct.ftqq.com/) 免费版即可）

### 1. 克隆项目

```bash
git clone https://github.com/Nesixyz/flight-monitor.git
cd flight-monitor
```

### 2. 安装依赖

```bash
# 飞猪 CLI (flyai search-flight)
npm install

# Python 仅标准库（json/subprocess/os/datetime/urllib/logging），无需 pip 安装
```

### 3. 配置

```bash
cp config.example.json config.json
```

编辑 `config.json` 填入（或用环境变量覆盖，优先级更高）：

```jsonc
{
  "origin": "Shanghai",                    // 出发城市（flyai 接受的三字码或英文城市名）
  "destination": "Paris",                  // 目的地
  "date_start": "2026-09-16",
  "date_end": "2026-10-02",
  "business_max_price": 13000,             // 公务舱含税价格上限
  "economy_max_price": 5000,               // 经济舱含税价格上限
  "max_transfers": 1,                      // 0=只看直飞, 1=可中转1次
  "serverchan_key": "SCTxxxxxxx",          // 也可通过环境变量 SERVERCHAN_KEY 注入
  "flyai_api_key": "sk-xxxxxxxxx",         // 也可通过环境变量 FLYAI_API_KEY 注入
  "query_interval_sec": 12,                // flyai 调用间隔，建议 ≥10
  "query_timeout_sec": 30,                 // 单次查询超时（网络不佳时调大）
  "schedule": {
    "phase1_end": "2026-08-20",            // 抓低价期结束日
    "phase2_end": "2026-09-05",            // 稳观察期结束日
    "monitor_end": "2026-09-15"            // 监控停止日（建议设在购票截止后）
  },
  "push": {
    "urgent_drop_pct": 5,                  // 降价百分比阈值，超过会带 🚨 标记
    "urgent_drop_abs": 500                 // 降价绝对阈值，超过会带 🚨 标记
  }
}
```

### 4. 立即运行一次（手动触发）

```bash
# MANUAL_RUN=1 强制忽略时段检查，立即执行
MANUAL_RUN=1 /usr/bin/python3 monitor.py

# 查看进度
tail -f monitor.log
```

成功后会：
- 微信收到完整格式推送（📊总结 + ⭐推荐航班 + 💼公务舱列表 + 💺经济舱列表 + ❌消失航班）
- 本地生成 `dashboard.html`，浏览器打开查看

---

## ☁️ GitHub Actions 云部署（推荐）

推到 GitHub 后无需本地开机即可 24/7 运行，状态文件自动 commit 回仓库持久化。

### 前置：仓库已公开（本仓库已公开）

免费账户的 GitHub Pages 仅支持公开仓库。如用私有仓库需 GitHub Pro。

### Step 1: 配置 Repository Secrets

**`Settings → Secrets and variables → Actions → Repository secrets → New repository secret`**：

| Secret 名称 | 填入内容 |
|---|---|
| `SERVERCHAN_KEY` | Server酱 SendKey，格式 `SCTxxxxxxx` |
| `FLYAI_API_KEY` | 飞猪开放平台 API Key，格式 `sk-xxxxxxx` |

### Step 2: 配置 Repository Variables

**同一页面切到 Variables 标签 → New repository variable**（URL 不是敏感信息，需渲染到看板和推送）：

| Variable 名称 | 填入内容 |
|---|---|
| `DASHBOARD_URL` | `https://你的用户名.github.io/flight-monitor/` （**末尾必须带 `/`**） |

### Step 3: 开启 GitHub Pages

`Settings → Pages → Build and deployment → Source` 下拉选择 **GitHub Actions**（不要选 Deploy from a branch）。页面自动保存，无需其他按钮。

### Step 4: 手动触发测试

顶部 **Actions → 左侧选「上海巴黎机票监控」 → 右侧 Run workflow**：
- Branch: `main`
- `manual_run`: 保持 `true`（手动触发默认忽略时段检查立即执行）
- 点绿色 **Run workflow**

运行约 **5~10 分钟**完成。成功后检查三个结果：
1. **微信**：收到推送消息
2. **在线看板**：访问 `https://你的用户名.github.io/flight-monitor/`，打开看到最新结果
3. **仓库主分支**：最新 commit 为 `chore: 更新监控状态 ...`（状态文件持久化）

### Step 5: 定时自动运行

Cron 已写在 `.github/workflows/monitor.yml`：
```
0 4,10,16,22 * * * (UTC)
= 北京时间 12:00, 18:00, 00:00, 06:00
```

Cron 触发时脚本内部会根据阶段决定是否真正执行，不属于当前阶段执行时段的触发会直接 skip（节省额度）。

---

## 📅 三阶段调度策略

| 阶段 | 日期范围 | 执行时段（北京时间） | 推送额度/天 | 说明 |
|---|---|---|---|---|
| ① **抓低价** | 项目启动 ~ 2026-08-20 | 00, 12, 18 点 | 3 条 | 早期白菜价捕捉，频次适中 |
| ② **稳观察** | 08-21 ~ 09-05 | 00, 12 点 | 2 条 | 价格平稳期，低频观察省额度 |
| ③ **冲刺期** | 09-06 ~ 09-15 | 00, 06, 12, 18 点 | 4 条 | 购票截止前高频扫最后低价 |
| 停止 | 09-16 及之后 | — | 0 | 自动退出，不再查询不消耗调用 |

**推送额度 = 本阶段每日执行次数**，满额当日停推（看板和状态持久化照常更新，等第二天额度重置）。

---

## 💡 关键设计 & 踩坑经验

### 1. 跨时区总时长修正

**问题**：飞猪起降时间用各城市本地时区（上海 UTC+8、巴黎 UTC+2 夏令时），若直接 `last_arr - first_dep`，MF8510→MF825（18时45分）会算成 12时45分，少整整 6 小时时区差。

**解法**：
```python
# 优先级：segment.duration 求和 > journey.totalDuration > 首末时间差（兜底）
Σ segment.duration（飞猪按本地时间校正，单位分钟） + Σ 中转停留分钟
```

### 2. Markdown 购票链接括号截断

**问题**：Server酱渲染 Markdown 的 `[text](url)` 时遇到 `)` 会提前截断，飞猪 jumpUrl query 中常含 `)`，导致跳转短链后半段缺失，点进去显示「获取跳转链接失败」。

**解法**（仅用于 Markdown，HTML `<a>` 不受影响）：
```python
def _md_safe_url(url):
    return url.replace(")", "%29").replace("(", "%28").replace(" ", "%20")
```

### 3. 风控日期误判为消失航班

**问题**：飞猪风控（403/Abnormal access）时当日返回空列表，如果直接和 notified 记录对比，会把「查询失败」的全部航班标成 ❌消失（最多一天报 200+ 条消失，实际只是飞猪风控）。

**解法**：`run_all()` 累积 `blocked_dates`，传给 `diff_flights`，这些日期下的已记录航班跳过消失判定。只有「查询成功且航班未出现」才标为消失。

### 4. 手动触发旁路 `MANUAL_RUN=1`

脚本正常只在 cron 指定时间点执行，但调试时或需要立即更新看板（如买完机票验证价格），用 `MANUAL_RUN=1` 强制 `should_run=True`。

GitHub Actions workflow_dispatch 的 `manual_run` 默认 `true`，自动注入 `MANUAL_RUN=1`；本地用 `MANUAL_RUN=1 python3 monitor.py`。

### 5. 定时环境 PATH 缺失找不到 python

Trae Schedule / crontab 的 shell 环境 PATH 有时只含 `/bin`，`python3` command not found。解决：
- 用绝对路径 `/usr/bin/python3`（macOS 系统 Python）
- 或 cron 第一行写 `PATH=/usr/local/bin:/usr/bin:/bin`（如果有自定义 python 安装）

本仓库 GitHub Actions runner 的 PATH 包含 python3，无需此处理。

### 6. GitHub Pages 单文件部署

**问题**：`actions/upload-pages-artifact@v3` 的 `path` 参数必须是**目录**（内部 `tar -cf`），直接填单个文件会报 `tar: dashboard.html: Not a directory`。

**解法**：复制到 `public/index.html` 后上传整个 `public/`：
```yaml
- run: |
    mkdir -p public
    cp dashboard.html public/index.html
- uses: actions/upload-pages-artifact@v3
  with:
    path: public
```

### 7. 飞猪 jumpUrl 是 webview 容器，需提取内层短链

**问题**：飞猪 API 返回的 `jumpUrl` 不是航班订座直链，而是 webview 容器 URL：
```
https://router.feizhu.com/multi/webview?url=https%3A%2F%2Frouter.feizhu.com%2Fws%2F4ATjQx
```
在浏览器中直接打开外层容器 URL 会跳到**当天搜索结果列表页**，而不是该航班的具体订座页。

**解法**：提取 `url` query 参数并解码，得到 `https://router.feizhu.com/ws/4ATjQx`（HTTP 302 自动跳转到该航班订座页）：
```python
def _extract_jump_url(url):
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    if "url" in params:
        return params["url"][0]
    return url
```

⚠️ 容易遗漏点：推送消息、看板推荐卡片、看板航班列表**三处**都要调用此函数，漏一处就会出现「点购票跳到搜索页」的问题。

---

## 📁 项目结构

```
flight-monitor/
├── monitor.py                    # 主脚本（查询/筛选/对比/推送/看板生成）
├── config.example.json           # 配置模板（含注释，可提交）
├── config.json                   # 本地实际配置（不提交，已 .gitignore）
├── dashboard.html                # 看板（每次运行自动生成）
├── package.json                  # flyai-cli 依赖声明
├── .github/workflows/monitor.yml # GitHub Actions 配置（cron + 手动触发 + Pages）
├── notified.json                 # 去重记录（提交，跨运行持久化）
├── push_history.json             # 每日推送计数（提交，跨运行持久化）
├── monitor.log                   # 本地日志（不提交）
├── node_modules/                 # npm 安装目录（不提交）
└── README.md
```

---

## 🔧 自定义指南

### 改航线 / 价格 / 日期范围

直接修改 `config.json` 或 `config.example.json`（fork 后自己用）。不需要改代码。

### 改阶段频率与日期

`monitor.py` 顶部 `get_current_phase` 函数里：
```python
phase1_hours = {0, 12, 18}     # 抓低价：3次/天
phase2_hours = {0, 12}         # 稳观察：2次/天
phase3_hours = {0, 6, 12, 18}  # 冲刺期：4次/天
```
同时改 workflow cron 两个列表对齐（workflow 是触发入口，内部阶段判断是守门人）。

### 改推荐航班筛选

`monitor.py` 顶部常量：
```python
RECOMMEND_BUSINESS_MAX_PRICE = 10000
RECOMMEND_ECONOMY_MAX_PRICE = 5000
RECOMMEND_DATE_START = "2026-09-23"
RECOMMEND_DATE_END = "2026-10-01"
```
`pick_recommended()` 按总时长排序，取第 0 条，如要最便宜就把排序 key 改成 `f["price"]`。

### 接入其他通知方式

参考 `send_serverchan` 在同位置加新发送函数（Bark、Telegram Bot、SMTP 邮件、钉钉/飞书 Webhook），然后在 main 推送逻辑中一起调用。如果不想写代码，可把 Server酱 SendKey 换成另一个服务的 Webhook URL，自己写个轻量转发服务接受 `title/desp` 参数再转投。

---

## 📜 License

MIT。随意 fork、修改，如有更优的监控策略或坑点，欢迎 Issue / PR。
