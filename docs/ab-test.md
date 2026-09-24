# A/B 测试：folocli vs feedparser 抓取层对比

> 求职项目证据之一。目标：证明「feedparser 自建抓取」在去掉 folocli 依赖后，日报质量不劣于原方案，且成本更低、更可控。

## 1. 背景与假设

- **版本 A（旧）**：folocli（海外托管聚合服务），36 个源全，依赖 `npx` + 账号，本机 Hermes cron 触发
- **版本 B（新）**：feedparser（直接抓 RSS），28 个源清单，GitHub Actions 海外 runner 触发，无账号无 npx

**核心假设**：feedparser 去掉 folocli 依赖（云端化收益）后，日报「有效信号命中」不劣于 folocli，成本更低、更可控。

**控制变量**：同一天、同一套 LLM 打分/分析 prompt、同一飞书推送渠道，只换抓取层。

## 2. 对比维度（指标）

| 维度 | 说明 | 怎么测 |
|---|---|---|
| 源覆盖 | 能稳定抓到的源数 | 抓取日志 |
| 抓取量 | 每日有效条目数 | 抓取日志 |
| 有效信号命中 | 日报 Top8 里「真值得深读」占比 | 人工标（Eval） |
| 漏报 | 真信号没进日报 | 人工标（Eval） |
| 成本 | LLM 调用次数 / 耗时 | 运行日志 |
| 稳定性 | 失败率、需人工干预次数 | 运行日志 |

## 3. 14 天实测数据（2026-09-09 ~ 09-23）

### 3.1 稳定性 —— 决定性差异

| | A (folocli) | B (feedparser) |
|---|---|---|
| 失败天数 | **4 天完全断档**（09-16~19 抓 0 条） | **0 天失败** |
| 失败原因 | folocli 服务端挂（cron 日志：`分页解析失败，停止`） | 无 |
| 源数稳定性 | 13~24 源剧烈波动 | 稳定 21/28 源 |
| 免开机 | ❌ 本机 cron，关机即断 | ✅ 云端，天天跑 |

**证据**：旧流程 cron 日志 `⚠️ 分页 1 解析失败，停止 → 抓取完成: 0 条`；新流程 logs/runs.jsonl 14 条记录每天 `飞书 True`、抓取 3286→3325 条波动 <1%。

### 3.2 抓取量 —— 「量大」是假象

- A：每天 382~731 条「当天条目」，但其中 **58% 是 arXiv 学术噪音**（arXiv×3 分类 + HN 占大头）
- B：日期过滤后 ~180 条，已砍 arXiv，更聚焦
- **结论**：A 的量大是噪音撑起来的，B 的量少是聚焦的结果

### 3.3 其他维度

| 维度 | A (folocli) | B (feedparser) | 结论 |
|---|---|---|---|
| 日报生成 | 4~9 分钟（本机） | 40 秒（云端） | B 快一个数量级 |
| 成本 | 本机 + npx | ¥0（GitHub Actions 免费） | B 完胜 |
| 依赖 | npx + folo 账号 + 本机常驻 | 纯 Python + 云端 | B 云化干净 |
| LLM 调用 | 打分1 + 分析8 + 趋势1 = 10 次 | 同 10 次 | 持平 |

## 4. 剩余 7 个失败源（B 的短板，待修）

| 源 | 失败原因 | 修复方案 |
|---|---|---|
| 36氪热榜 | rsshub.app 403 | 换 36氪官方 RSS |
| VentureBeat AI | 429 反爬 | 换 UA + 延迟重试 |
| Karpathy Twitter/YouTube、YC YouTube、elvis Twitter | rsshub.app 404/403 | 自建 RSSHub 配 cookie |
| GitHub Trending | rsshub.app 403 | 自建 RSSHub 或换源 |

## 5. 收获与注意点（持续更新）

### 最终结论（2026-09-24）
**feedparser 新方案全面胜出**。决定性证据是稳定性：旧流程依赖第三方托管服务 folocli，服务一不稳定就断档 4 天（09-16~19 抓 0 条）；新流程直接抓 RSS + 云端部署，14 天零失败。配合成本（¥0 vs 本机+npx）、可维护（纯 Python vs 依赖账号）、聚焦度（砍 arXiv 噪音）三项，结论清晰：**可完全替换 folocli，停掉旧 cron**。

### 关键结论
- **folocli 的本质是「海外托管聚合」**，替国内用户绕墙抓国外源；feedparser 从国内直连国外源必失败（arXiv WinError10054 / HF 超时 / VB 429 / rsshub.app 全超时）
- **海外部署（GitHub Actions）是抓国外源的正解**，也顺带解决了「免开机」和「免 npx 依赖」
- 失败的 7 个源里，arXiv 是学术噪音（砍了反而提升日报聚焦度）；Twitter/YouTube/GitHub Trending 对 AI PM 日报价值有限

### 踩过的坑（值得记住）
1. feedparser 的 `_parse_date()` 返回 `struct_time` 不是 `datetime`，直接 `.astimezone()` 会静默导致日期过滤全军覆没（3286 条 → 0 条）—— 正确做法是用 `*_parsed` 字段转 `datetime(*st[:6], tzinfo=utc)`
2. rsshub.app 公共实例国内不可达、且对 twitter/youtube/github 等 route 有限制（403/404），需要自建实例配 cookie
3. GitHub artifact 下载遇 302 重定向，urllib 会把 Authorization 带到 Azure blob 被 403，需用 curl
4. Windows 快速连 GitHub API 会触发 socket 端口耗尽（WinError 10048），需加延时重试

## 6. 待办
- [x] 跑满 3-5 天（已跑 14 天）
- [x] 得出最终结论（feedparser 胜出，2026-09-24）
- [ ] 停掉旧 folocli cron（避免双跑浪费 LLM）
- [ ] 补「有效信号命中」「漏报」的人工 Eval（唯一还没做的核心项，需用户标）
- [ ] 修 36氪（换官方 RSS）、VentureBeat（反爬）
- [ ] 决定是否自建 RSSHub 救 Twitter/YouTube/GitHub Trending
