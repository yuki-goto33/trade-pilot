"""Google News RSS でユニバース銘柄の銘柄別ニュース（直近1日）を取得する。

- クエリ: 「"社名" OR "証券コード"」 + when:1d を URL エンコード
  （社名は universe の news_name があればそちらを優先。例: キオクシア）
- URL: https://news.google.com/rss/search?q=<enc>&hl=ja&gl=JP&ceid=JP:ja
- feedparser 使用、リクエスト間 2 秒
- 出力: data/news_google.json
"""
import sys
import time
from urllib.parse import quote

import feedparser

from common import Timer, now_jst_iso, parse_universe_arg, print_summary, save_json
from universe import load_universe

SOURCE = "news_google"
SLEEP_SEC = 2.0


def _entry_to_dict(e) -> dict:
    return {
        "title": e.get("title"),
        "link": e.get("link"),
        "published": e.get("published"),
        "source": (e.get("source") or {}).get("title"),
    }


def fetch(universe) -> dict:
    results = {}
    failed = []
    for i, s in enumerate(universe):
        if i > 0:
            time.sleep(SLEEP_SEC)
        code, name = s["code"], s.get("news_name", s["name"])
        query = f'"{name}" OR "{code}" when:1d'
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ja&gl=JP&ceid=JP:ja"
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                raise RuntimeError(f"feed parse error: {feed.get('bozo_exception')}")
            entries = [_entry_to_dict(e) for e in feed.entries]
            results[code] = {"name": name, "query": query, "count": len(entries), "items": entries}
            print(f"  {code} {name}: {len(entries)} 件")
        except Exception as e:
            print(f"  {code} 失敗: {e}", file=sys.stderr)
            failed.append(code)

    if not results:
        raise RuntimeError("全銘柄の取得に失敗")
    return {"fetched_at": now_jst_iso(), "failed": failed, "news": results}


def main(universe_path=None) -> dict:
    universe = load_universe(universe_path)
    with Timer() as t:
        try:
            data = fetch(universe)
            out = save_json("news_google.json", data)
            count = sum(v["count"] for v in data["news"].values())
            note = f"{len(data['news'])}/{len(universe)} 銘柄, 保存先 {out.name}"
            summary = {"source": SOURCE, "ok": True, "count": count, "note": note}
        except Exception as e:
            summary = {"source": SOURCE, "ok": False, "count": 0, "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main(parse_universe_arg(__doc__))["ok"] else 1)
