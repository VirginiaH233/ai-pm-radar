#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py — AI PM 情报雷达 · 抓取层（feedparser 版）

读 config/rss_sources.yaml 的 RSS 清单，并发抓取，输出标准化条目。

用法:
    python fetch.py                 # 抓全部源，控制台打印摘要 + 存 output/
    python fetch.py --json          # 额外打印完整 JSON 到 stdout
    python fetch.py --since 1       # 只保留最近 N 天发布的条目
    python fetch.py --max-workers 8 # 并发数

依赖: 标准库 + feedparser（pip install feedparser）
"""
import sys, os, json, time, ssl, argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

try:
    import yaml
except ImportError:
    yaml = None

import feedparser

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(BASE, "config", "rss_sources.yaml")
OUTPUT = os.path.join(BASE, "output")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
      "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.9"}

# 忽略证书校验（部分源 SSL 配置不规范）
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def load_sources(path=CONFIG):
    if not os.path.exists(path):
        print(f"❌ 源清单不存在: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        if yaml:
            data = yaml.safe_load(f)
        else:
            print("⚠️ 未装 pyyaml，尝试简易解析", file=sys.stderr)
            data = simple_yaml(f.read())
    return data.get("sources", [])


def simple_yaml(text):
    """无 pyyaml 时的兜底（仅支持 name:/url: 两字段的简单格式）"""
    import re
    sources = []
    for block in text.split("- {"):
        m_name = re.search(r'name:\s*"([^"]+)"', block)
        m_url = re.search(r'url:\s*"([^"]+)"', block)
        m_cat = re.search(r'category:\s*"([^"]+)"', block)
        if m_name and m_url:
            sources.append({"name": m_name.group(1), "url": m_url.group(1),
                            "category": m_cat.group(1) if m_cat else ""})
    return {"sources": sources}


def _parse_time(entry):
    """从 entry 提取发布时间（返回 timezone-aware datetime 或 None）"""
    # 优先用 feedparser 已解析的 *_parsed（time.struct_time）
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        raw = entry.get(key)
        if raw:
            try:
                return datetime(*raw[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    # 兜底：原始字符串（feedparser._parse_date 返回 struct_time）
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if isinstance(raw, str):
            try:
                st = feedparser._parse_date(raw)
                if st:
                    return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def fetch_one(src, timeout=25):
    """抓单个源，返回 (name, status, entries)"""
    name, url = src.get("name", "?"), src.get("url", "")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            raw = r.read(5_000_000)
        d = feedparser.parse(raw)
        if d.bozo and not d.entries:
            return name, {"ok": False, "reason": "bozo: " + str(d.bozo_exception)[:80]}, []
        entries = []
        for e in d.entries:
            entries.append({
                "title": (e.get("title") or "").strip(),
                "url": e.get("link") or "",
                "summary": (e.get("summary") or e.get("description") or "")[:500],
                "published_at": _parse_time(e),
                "source": name,
                "category": src.get("category", ""),
            })
        return name, {"ok": True, "reason": ""}, entries
    except Exception as e:
        return name, {"ok": False, "reason": str(e)[:80]}, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="stdout 输出完整 JSON")
    ap.add_argument("--since", type=int, default=0, help="只保留最近 N 天发布的条目")
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()

    sources = load_sources(args.config)
    print(f"📡 加载 {len(sources)} 个源，并发 {args.max_workers}，开始抓取...\n", flush=True)

    t0 = time.time()
    results = {}  # name -> {"ok", "reason", "count", "latest", "entries"}
    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futs = {ex.submit(fetch_one, s): s for s in sources}
        for fut in as_completed(futs):
            name, status, entries = fut.result()
            # since 过滤
            if args.since > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
                entries = [e for e in entries
                           if e["published_at"] and e["published_at"] >= cutoff]
            latest = ""
            if entries:
                ts = [e["published_at"] for e in entries if e["published_at"]]
                if ts:
                    latest = max(ts).strftime("%Y-%m-%d %H:%M")
            results[name] = {"ok": status["ok"], "reason": status["reason"],
                             "count": len(entries), "latest": latest,
                             "entries": entries}

    # 控制台摘要（按条目数降序）
    ok = sum(1 for r in results.values() if r["ok"])
    print(f"✅ 完成，耗时 {time.time()-t0:.1f}s | 成功 {ok}/{len(sources)}\n")
    print(f"{'源':<24} {'状态':<8} {'条目':>5}  最新时间")
    print("-" * 60)
    for name in sorted(results, key=lambda n: -results[n]["count"]):
        r = results[name]
        mark = "OK" if r["ok"] else f"FAIL({r['reason'][:20]})"
        print(f"{name:<24} {mark:<8} {r['count']:>5}  {r['latest']}")

    # 保存 JSON
    os.makedirs(OUTPUT, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT, f"{day}_raw.json")
    total_entries = sum(r["count"] for r in results.values())
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(sources),
        "ok_count": ok,
        "total_entries": total_entries,
        "sources": {name: {"ok": r["ok"], "count": r["count"],
                           "latest": r["latest"], "reason": r["reason"],
                           "entries": r["entries"]}
                    for name, r in results.items()},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n💾 结果已存: {out_path}（共 {total_entries} 条）")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
