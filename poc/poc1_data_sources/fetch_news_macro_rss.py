"""マクロ・市況ニュース RSS（NHK 経済 + Yahoo!ニュース経済）を取得する。

- NHK 経済: https://www.nhk.or.jp/rss/news/cat5.xml（個人利用のみ可）
- Yahoo!ニュース経済: https://news.yahoo.co.jp/rss/categories/business.xml
- 出力: data/news_macro_rss.json
"""
import sys
import time

import feedparser

from common import Timer, now_jst_iso, print_summary, save_json

SOURCE = "news_macro_rss"
SLEEP_SEC = 2.0

FEEDS = {
    "nhk_keizai": "https://www.nhk.or.jp/rss/news/cat5.xml",
    "yahoo_business": "https://news.yahoo.co.jp/rss/categories/business.xml",
}


def _entry_to_dict(e) -> dict:
    return {
        "title": e.get("title"),
        "link": e.get("link"),
        "published": e.get("published"),
        "summary": e.get("summary"),
    }


def fetch() -> dict:
    results = {}
    failed = []
    for i, (key, url) in enumerate(FEEDS.items()):
        if i > 0:
            time.sleep(SLEEP_SEC)
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                raise RuntimeError(f"feed parse error: {feed.get('bozo_exception')}")
            entries = [_entry_to_dict(e) for e in feed.entries]
            results[key] = {"url": url, "count": len(entries), "items": entries}
            print(f"  {key}: {len(entries)} 件")
        except Exception as e:
            print(f"  {key} 失敗: {e}", file=sys.stderr)
            failed.append(key)

    if not results:
        raise RuntimeError("全フィードの取得に失敗")
    return {"fetched_at": now_jst_iso(), "failed": failed, "feeds": results}


def main() -> dict:
    with Timer() as t:
        try:
            data = fetch()
            out = save_json("news_macro_rss.json", data)
            count = sum(v["count"] for v in data["feeds"].values())
            note = f"{len(data['feeds'])}/{len(FEEDS)} フィード, 保存先 {out.name}"
            summary = {"source": SOURCE, "ok": True, "count": count, "note": note}
        except Exception as e:
            summary = {"source": SOURCE, "ok": False, "count": 0, "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
