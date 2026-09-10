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

## 3. 已知数据（首日）

| 维度 | A (folocli) | B (feedparser) | 结论 |
|---|---|---|---|
| 源数 | 36 | 28（砍了 arXiv×3 + 排除带 token 的桥接） | B 源少但聚焦 |
| 本地抓取 | 36 全（托管） | 14/28（国内直连国外源失败） | **B 必须海外部署** |
| 海外抓取 | — | 21/28（剩 7 个是 rsshub.app 限制） | 海外救回 7 个网络失败源 |
| 日报生成 | ~9 分钟（agent 版）/ 4 分钟（脚本版） | 42 秒（云端） | B 快一个数量级 |
| LLM 调用 | 打分1 + 逐条分析8 + 趋势1 = 10 次 | 同 10 次 | 持平 |
| 依赖 | npx + folo 账号 + 本机常驻 | 纯 Python + GitHub Actions | B 云化干净 |

## 4. 剩余 7 个失败源（B 的短板，待修）

| 源 | 失败原因 | 修复方案 |
|---|---|---|
| 36氪热榜 | rsshub.app 403 | 换 36氪官方 RSS |
| VentureBeat AI | 429 反爬 | 换 UA + 延迟重试 |
| Karpathy Twitter/YouTube、YC YouTube、elvis Twitter | rsshub.app 404/403 | 自建 RSSHub 配 cookie |
| GitHub Trending | rsshub.app 403 | 自建 RSSHub 或换源 |

## 5. 收获与注意点（持续更新）

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
- [ ] 跑满 3-5 天，补「有效信号命中」和「漏报」的人工 Eval 数据
- [ ] 修 36氪（换官方 RSS）、VentureBeat（反爬）
- [ ] 决定是否自建 RSSHub 救 Twitter/YouTube/GitHub Trending
- [ ] 得出最终结论：是否完全替换 folocli
