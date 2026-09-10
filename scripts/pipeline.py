#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py — AI PM 情报雷达 · 完整日报流程

抓取(feedparser) → 黑名单 → LLM打分选Top8 → 抓正文 → 逐条深度分析 + 趋势脉动 → 生成日报md → 推飞书

用法:
    python scripts/pipeline.py [YYYY-MM-DD] [--dry-run]
      YYYY-MM-DD  要生成日报的日期（默认昨天，北京时间）
      --dry-run   只跑抓取+打分，不抓正文不生成日报不推送

环境变量（云端走 GitHub Secrets，本地可 export 注入）:
    DEEPSEEK_API_KEY   DeepSeek API key（必填）
    FEISHU_WEBHOOK     飞书群机器人 webhook（可选，不设则跳过推送）
    FEISHU_SECRET      飞书签名 secret（可选）

依赖: feedparser + pyyaml（详见 fetch.py）
"""
import os, sys, re, json, time, html, argparse
import urllib.request, urllib.parse
import hmac, hashlib, base64
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import load_sources, fetch_one, _parse_time

TZ = timezone(timedelta(hours=8))          # 北京时间
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(BASE, "output")
TOP_N = 8
SCORE_LIMIT = 500

# ============ 工具 ============

def log(msg):
    print(f"[{time.time()-T0:6.1f}s] {msg}", flush=True)

def get_env(key):
    v = os.environ.get(key, "").strip()
    return v

def llm_call(system, user, max_tokens=4000, temperature=0.3):
    """调用 DeepSeek API"""
    key = get_env("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置（环境变量）")
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": temperature,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]

def clean_html(raw):
    if not raw:
        return ""
    raw = re.sub(r"<script.*?</script>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<style.*?</style>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p>|</div>|</h[1-6]>|</li>|</tr>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n\n", raw)
    return raw.strip()

# ============ Step 1: 抓取 ============

def fetch_all(sources, max_workers=8):
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_one, s): s for s in sources}
        for fut in futs:
            name, status, entries = fut.result()
            if status["ok"]:
                results[name] = entries
    return results

# ============ Step 2: 黑名单 ============

R2 = re.compile(r"招聘|招人|岗位|职位|面试|简历|薪资|offer", re.I)
R3 = re.compile(r"报名|注册|门票|线下|沙龙|峰会|直播|课程|培训|workshop", re.I)
R4 = re.compile(r"融资|估值|上市|IPO|投资|收购|并购", re.I)
R4X = re.compile(r"产品|模型|技术|应用|用户", re.I)

def blacklist_filter(entries):
    filtered, dropped = [], []
    seen_urls, seen_titles = set(), []
    for it in entries:
        title, url = it["title"] or "", it["url"] or ""
        nt = re.sub(r"\s+", "", title).lower()
        if url in seen_urls or any(nt == t or (nt and t and nt in t) for t in seen_titles[-200:]):
            dropped.append((it, "重复")); continue
        seen_urls.add(url); seen_titles.append(nt)
        if R2.search(title): dropped.append((it, "招聘")); continue
        if R3.search(title): dropped.append((it, "活动")); continue
        if R4.search(title) and not R4X.search(title): dropped.append((it, "纯融资")); continue
        filtered.append(it)
    return filtered, dropped

# ============ Step 3: LLM 打分选 Top8 ============

SYS_PROMPT_SCORE = """你是 AI 产品经理行业资讯筛选专家。

你的任务：从以下资讯列表中，选出今天最重要的 8 条新闻。

选择标准：
1. 优先选对 AI PM 决策有直接影响的事件（核心人事变动、API定价变动、重大产品发布/关停、行业格局变化）
2. 同一新闻事件（即使标题不同、语言不同、来源不同）只选一条，选信息最完整的一手信源
3. 对每个入选条目，列出该事件的其他报道来源（如有），填入 cross_refs 字段（来源名称列表，无则空）
4. 类型尽量多元，不要全是同类新闻
5. 未被选入 Top 8 但值得关注的条目，放入 notable 列表

输出严格按 JSON，不要任何额外文字：
{
  "selected": [{"id": "序号", "title": "原标题", "reason": "≤30字入选理由", "cross_refs": "其他来源（如36氪、量子位），无则空"}, ...],
  "notable": [{"id": "序号", "title": "原标题", "reason": "≤20字值得关注理由"}, ...]
}"""

def score_top8(scored_input):
    title_lines = "\n".join("%d | %s | %s" % (i+1, it["source"][:20], (it["title"] or "")[:80])
                            for i, it in enumerate(scored_input))
    resp = llm_call(SYS_PROMPT_SCORE, title_lines, max_tokens=4000)
    m = re.search(r"\{[\s\S]*\}", resp)
    raw_json = m.group(0) if m else resp
    try:
        selection = json.loads(raw_json)
    except Exception:
        selection = json.loads(raw_json.replace("```json", "").replace("```", "").strip())

    id_map = {str(i+1): it for i, it in enumerate(scored_input)}
    top_items, notable_items = [], []
    for sel in (selection.get("selected") or [])[:TOP_N]:
        it = id_map.get(sel["id"])
        if it:
            it["score"] = 5
            it["score_reason"] = sel.get("reason", "")
            it["cross_refs"] = sel.get("cross_refs", "")
            top_items.append(it)
    for sel in (selection.get("notable") or [])[:20]:
        it = id_map.get(sel["id"])
        if it:
            it["score"] = 3
            notable_items.append(it)
    return top_items, notable_items

# ============ Step 4: 抓正文 ============

def fetch_content(it):
    """抓单条正文：feed 摘要 → requests 抓原文 → 失败兜底"""
    full, status = None, {"ok": False, "level": "failed"}
    # L1: feed 自带 summary（够长就用）
    summary = (it.get("summary") or "").strip()
    if len(summary) >= 150:
        full, status = clean_html(summary), {"ok": True, "level": "summary"}
    # L2: requests 抓原文
    if not full or len(full) < 300:
        try:
            req = urllib.request.Request(it["url"], headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw_html = resp.read().decode("utf-8", errors="replace")
                full = clean_html(raw_html)[:4000]
                status = {"ok": True, "level": "direct"}
        except Exception:
            pass
    if not full or len(full) < 50:
        full = summary + " " + (it["url"] or "")
        status = {"ok": False, "level": "failed"}
    return {**it, "full_text": full, "fetch_status": status}

# ============ Step 5: 深度分析 + 趋势 ============

SYS_PROMPT_DEEP = """你是经验丰富的 AI 产品经理导师，面向正在转行 AI PM 的入门者。
你只需深度分析下面这一条资讯。不要总结趋势，只聚焦这条。

输出严格按以下结构：

### {i}. {title} ⭐{score}
**来源**：{source}
**一句话摘要**：{20字以内}

🔍 **入门解读**：{120字以内，入门者能看懂。必须引用原文中至少一个具体数字、名称或数据点，禁止纯泛化概括。如果正文信息不足，可结合你训练数据中关于此事件/产品/公司的最新公开信息辅助说明}

🎯 **PM 视角**：{100字以内，讲清这条新闻和AI PM工作/学习/求职的具体关系}

✅ **行动建议**：{一个30分钟内可完成的小动作，禁止写空话}

🏷️ **知识标签**：{3-5个 #标签}

注意：标题必须是输入中给出的原标题原文，禁止改动、禁止添加 Markdown 链接（链接由系统自动补全）。全文中文，平实直接，不要官话套话。"""

SYS_PROMPT_TREND = """你是 AI 产品经理行业观察家。根据今日全部条目的打分分布和低分条目列表，提炼趋势信号。

输出严格按此结构：

## 今日信号
用≤30字的一句话总结今天所有新闻共同反映的行业趋势。
严格写一行：今日信号：XXXX

## 高频主题（≤3个）
- **{主题}**（{N}条）：{一句话}。代表：{条目}

## 值得留意
{最多8条 ⭐3 条目，每条格式：- {标题}：{15字价值点}}"""

def deep_analyze(content_results):
    item_analyses = []
    for i, it in enumerate(content_results, 1):
        body = it["full_text"][:3500] if it["full_text"] else "（正文抓取失败）"
        fetch_note = ""
        if not it["fetch_status"]["ok"]:
            fetch_note = "\n⚠️ 正文抓取失败（%s），请结合你训练数据中关于此事件的最新公开信息辅助分析。" % it["fetch_status"].get("level", "unknown")
        cross_note = ""
        if it.get("cross_refs") and str(it["cross_refs"]).strip():
            cross_note = "\n交叉信源：此事件也被以下来源报道——%s。可在分析中交叉引用多个来源的信息。" % it["cross_refs"]
        user = "以下是一条需要分析的资讯：\n\n标题：%s\n来源：%s\nURL：%s\n重要性：⭐%d%s\n正文：\n%s%s" % (
            it["title"], it["source"], it["url"], it["score"], cross_note, body, fetch_note)
        resp = llm_call(SYS_PROMPT_DEEP, user, max_tokens=2000)
        item_analyses.append(resp)
        log(f"  第{i}/{len(content_results)}条: {it['title'][:25]}... 分析完成 ({len(resp)}字)")
    return item_analyses

def trend_analysis(notable_items, dist, scored):
    s3_text = "\n".join("- %s (%s)" % (it["title"], it["source"]) for it in notable_items[:20])
    user_trend = "今日打分分布：%s\n共计 %d 条\n\n⭐3 条目列表：\n%s" % (
        json.dumps(dist, ensure_ascii=False), len(scored), s3_text)
    return llm_call(SYS_PROMPT_TREND, user_trend, max_tokens=2000)

# ============ Step 6: 生成日报 ============

def build_daily_md(DATE, signal, deep_part, trend_part, stats, now_str):
    return (
        "# 📡 AI PM Radar · %s\n\n"
        "> **今日信号**：%s\n\n"
        "---\n\n"
        "## 🔥 深度解读\n\n"
        "%s\n\n"
        "---\n\n"
        "## 📈 今日趋势脉动\n\n"
        "%s\n\n"
        "---\n\n"
        "## 📊 今日数据\n\n"
        "| 指标 | 数值 |\n"
        "|------|------|\n"
        "| 抓取原始 | %d 条 |\n"
        "| 黑名单过滤 | 丢弃 %d 条 |\n"
        "| 全量打分 | %d 条 |\n"
        "| 深度分析 | %d 条 |\n"
        "| 正文抓取失败 | %d 条 |\n\n"
        "---\n\n"
        "*Generated by Radar Pipeline v1.0 · %s*\n"
    ) % (DATE, signal, deep_part, trend_part, stats["raw"], stats["dropped"],
         stats["scored"], stats["analyzed"], stats["failed"], now_str)

# ============ Step 7: 推飞书 ============

def feishu_send(payload, webhook, secret):
    ts = str(int(time.time()))
    if secret:
        s2s = f"{ts}\n{secret}"
        sign = base64.b64encode(hmac.new(s2s.encode(), digestmod=hashlib.sha256).digest()).decode()
        url = f"{webhook}?timestamp={ts}&sign={urllib.parse.quote(sign)}"
    else:
        url = webhook
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")

def build_card(md_text, date):
    # 简化：日报转飞书卡片（标题 + markdown 正文，截断到合理长度）
    lines = md_text.split("\n")
    title = date
    for l in lines:
        if l.startswith("# "):
            title = l[2:].strip()
            break
    body = "\n".join(lines[1:])[:6000]  # 飞书卡片 markdown 长度限制
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": [{"tag": "markdown", "content": body}],
    }
    return {"msg_type": "interactive", "card": card}

def push_feishu(md_text, date):
    webhook = get_env("FEISHU_WEBHOOK")
    if not webhook:
        log("⚠️ 未设 FEISHU_WEBHOOK，跳过飞书推送")
        return None  # None = 跳过
    secret = get_env("FEISHU_SECRET")
    payload = build_card(md_text, date)
    try:
        result = feishu_send(payload, webhook, secret)
        if '"code":0' in result or '"StatusCode":0' in result:
            log("✅ 飞书推送成功")
            return True
        else:
            log(f"❌ 飞书推送失败: {result[:100]}")
            return False
    except Exception as e:
        log(f"❌ 飞书推送异常: {e}")
        return False


def write_run_log(stats):
    """追加一条运行日志到 logs/runs.jsonl（操作日志/可观测性）"""
    log_dir = os.path.join(BASE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "runs.jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(stats, ensure_ascii=False) + "\n")

# ============ 主流程 ============

def main():
    global T0
    T0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    DATE = args.date
    if not DATE:
        DATE = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    sources = load_sources()
    log(f"🚀 Radar Pipeline 启动 · 生成 {DATE} 日报")

    # 抓取
    log(f"抓取 {len(sources)} 个源...")
    by_source = fetch_all(sources)
    all_entries = []
    for name, entries in by_source.items():
        all_entries.extend(entries)
    log(f"抓取完成: {len(all_entries)} 条（{len(by_source)} 个源成功）")

    # 按日期过滤（DATE ± 1 天）
    def _dt(pa):
        return pa if isinstance(pa, datetime) else (_parse_time({"published": pa}) if pa else None)
    target = datetime.strptime(DATE, "%Y-%m-%d").replace(tzinfo=TZ)
    day_entries = [e for e in all_entries
                   if e["published_at"] and abs((e["published_at"].astimezone(TZ) - target).days) <= 1]
    if len(day_entries) < 10:
        log(f"⚠️ 当天条目 <10（{len(day_entries)}），放宽到最近 3 天")
        day_entries = [e for e in all_entries
                       if e["published_at"] and abs((e["published_at"].astimezone(TZ) - target).days) <= 3]
    log(f"{DATE} 过滤后: {len(day_entries)} 条")

    # 黑名单
    filtered, dropped = blacklist_filter(day_entries)
    log(f"黑名单过滤: 保留 {len(filtered)} / 丢弃 {len(dropped)}")

    # LLM 打分
    scored_input = filtered[:SCORE_LIMIT]
    log(f"LLM 打分选 Top {TOP_N} 中（{len(scored_input)} 条）...")
    top_items, notable_items = score_top8(scored_input)
    log(f"LLM 选中 {len(top_items)} 条")

    if args.dry_run:
        log("🛑 dry-run 模式，停止")
        for it in top_items:
            log(f"  → {it['title'][:50]} [{it['source'][:15]}]")
        return

    # 抓正文
    log(f"抓取 Top {len(top_items)} 正文...")
    content_results = [fetch_content(it) for it in top_items]
    for i, it in enumerate(content_results, 1):
        log(f"  第{i}/{len(content_results)}条: {it['title'][:30]}... {it['fetch_status']['level']} ({len(it['full_text'])}字)")

    # 深度分析 + 趋势
    log(f"逐条深度分析（{len(content_results)} 条）...")
    item_analyses = deep_analyze(content_results)
    log("趋势脉动...")
    dist = {5: len(top_items), 3: len(notable_items)}
    scored = top_items + notable_items + scored_input
    trend = trend_analysis(notable_items, dist, scored)

    # 提取今日信号
    signal = ""
    m = re.search(r"今日信号[：:]\s*(.+?)(?:\n|$)", trend)
    if m:
        signal = m.group(1).strip().strip("#* >\"'").strip()

    # 拼接深度解读（注入真实 URL）
    deep_parts = []
    for i, (analysis_text, it) in enumerate(zip(item_analyses, content_results), 1):
        lines = analysis_text.split("\n")
        for li, line in enumerate(lines):
            m_url = re.match(r"^###\s*(\d+)?\.?\s*(.+?)\s*⭐(\d+)$", line)
            if m_url:
                lines[li] = "### %d. [%s](%s) ⭐%s" % (i, it["title"], it["url"], m_url.group(3))
                break
        else:
            lines.insert(0, "### %d. [%s](%s) ⭐%d" % (i, it["title"], it["url"], it["score"]))
        deep_parts.append("\n".join(lines))
    deep_part = "\n\n---\n\n".join(deep_parts)

    # 趋势脉动去头
    trend_part = re.sub(r"##\s*今日信号\s*\n.*?(?=\n##|\n\n##|$)", "", trend, flags=re.S).strip()
    trend_part = re.sub(r"^今日信号[：:].*$", "", trend_part, flags=re.M).strip()

    failed_cnt = sum(1 for c in content_results if not c["fetch_status"]["ok"])
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    daily_md = build_daily_md(DATE, signal, deep_part, trend_part, {
        "raw": len(all_entries), "dropped": len(dropped),
        "scored": len(scored), "analyzed": len(content_results), "failed": failed_cnt,
    }, now_str)

    os.makedirs(OUTPUT, exist_ok=True)
    out_path = os.path.join(OUTPUT, f"{DATE}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(daily_md)
    log(f"✅ 日报已保存: {out_path}")

    # 推飞书
    pushed = push_feishu(daily_md, DATE)

    duration = round(time.time() - T0, 1)
    log(f"🎉 全部完成，总耗时 {duration} 秒")

    # 写操作日志（可观测性，累积历史）
    write_run_log({
        "ts": datetime.now(TZ).isoformat(),
        "date": DATE,
        "sources_ok": len(by_source),
        "sources_total": len(sources),
        "entries_fetched": len(all_entries),
        "after_date_filter": len(day_entries),
        "after_blacklist": len(filtered),
        "scored_input": len(scored_input),
        "top_n": len(top_items),
        "analyzed": len(content_results),
        "fetch_failed": failed_cnt,
        "duration_s": duration,
        "feishu_pushed": pushed,
    })


if __name__ == "__main__":
    main()
