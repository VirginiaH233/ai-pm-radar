# AI PM 情报雷达（ai-pm-radar）

领域可配置的 AI 情报雷达抓取层。每天定时抓取 RSS 源清单 → LLM 筛选打分 → 深度分析 → 生成日报 → 推送飞书/微信。

## 现状（v1 · 抓取层）

- `config/rss_sources.yaml` — RSS 源清单（28 个源，按中文媒体/英文媒体/官方博客/社区榜单/人物观点 5 类）
- `scripts/fetch.py` — feedparser 抓取脚本（并发、标准化输出、存 `output/`）
- `.github/workflows/radar.yml` — GitHub Actions 定时抓取（UTC 01:20 = 北京 09:20）

## 为什么用 GitHub Actions 而不是本机 cron

1. **海外网络**：runner 在海外，能抓国内直连失败的国外源（arXiv、VentureBeat、HuggingFace、Twitter 等）
2. **免开机**：不依赖本机常驻，到点自动跑
3. **零成本**：免费额度内，纯 feedparser 无重依赖

## 本地运行

```bash
pip install feedparser pyyaml
python scripts/fetch.py            # 抓全部源
python scripts/fetch.py --since 1  # 只看最近 1 天
```

## 后续（v2）

- 接黑名单过滤 + LLM 打分（复用 AI-PM-Agent-System 的 prompt）
- 接深度分析 + 日报生成
- 接飞书/微信推送
- 领域配置化（给多个人开定制雷达）
