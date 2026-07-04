"""ヒストリカル用: Google News RSS の日付指定検索で過去の見出しをキャッシュする。

as-of 日 D（シグナル生成日）ごとに「D の朝に見えていた見出し = D-3〜D-1 の公開分」を
取得する。クエリは `"銘柄名" OR "コード" after:(D-3) before:D` を URL エンコードして
https://news.google.com/rss/search に投げる（before: は当日を含まないため、
before:D で D-1 までの見出しになる）。

- 保存先: data/news_history/<code>/<YYYY-MM-DD>.json（生データ全件。
  件数を最大10件に絞るのはコンテキスト組み立て側の責務）
- キャッシュ済み（ファイル存在）はスキップ → 再実行安全・チャンク実行可能
- リクエスト間 3 秒 + 429/ブロック/パース失敗時は指数バックオフでリトライ

使い方:
    ../../../.venv/bin/python fetch_news_range.py --start 2026-04-01 --end 2026-04-30
    ../../../.venv/bin/python fetch_news_range.py --start 2026-04-01 --end 2026-04-10 --codes 7203
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import pandas as pd
import requests

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
REPO_ROOT = POC3_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data"
NEWS_HISTORY_DIR = DATA_DIR / "news_history"

sys.path.insert(0, str(POC3_DIR.parent / "poc1_data_sources"))
from universe import UNIVERSE  # noqa: E402

JST = timezone(timedelta(hours=9))

SLEEP_SEC = 3.0          # 通常のリクエスト間隔
LOOKBACK_DAYS = 3        # D の朝に見える = D-3 〜 D-1 の見出し
MAX_RETRIES = 4
BACKOFF_BASE_SEC = 30.0  # 429/ブロック時の初期バックオフ

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _entry_to_dict(e) -> dict:
    return {
        "title": e.get("title"),
        "link": e.get("link"),
        "published": e.get("published"),
        "source": (e.get("source") or {}).get("title"),
    }


def build_query(name: str, code: str, asof: str) -> str:
    d = datetime.strptime(asof, "%Y-%m-%d").date()
    after = (d - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return f'"{name}" OR "{code}" after:{after} before:{asof}'


def fetch_one(name: str, code: str, asof: str) -> dict:
    """1銘柄 × 1 as-of 日の見出しを取得する（リトライ込み）。"""
    query = build_query(name, code, asof)
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ja&gl=JP&ceid=JP:ja"

    last_err = None
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            delay = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            print(f"    retry {attempt}/{MAX_RETRIES - 1} in {delay:.0f}s ({last_err})",
                  file=sys.stderr)
            time.sleep(delay)
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        except requests.RequestException as e:
            last_err = f"request error: {e}"
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            last_err = f"feed parse error: {feed.get('bozo_exception')}"
            continue
        items = [_entry_to_dict(e) for e in feed.entries]
        return {
            "code": code,
            "name": name,
            "asof": asof,
            "query": query,
            "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
            "count": len(items),
            "items": items,
        }
    raise RuntimeError(f"リトライ上限到達: {last_err}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="終了日 YYYY-MM-DD（含む）")
    parser.add_argument("--codes", nargs="*", default=None,
                        help="対象銘柄コード（省略時は universe 全銘柄）")
    args = parser.parse_args()

    universe = [s for s in UNIVERSE if args.codes is None or s["code"] in args.codes]
    if not universe:
        parser.error(f"対象銘柄がありません: {args.codes}")

    # 平日（月〜金）を対象。日本の祝日も含まれるが、営業日はその部分集合なので
    # シグナル生成側で必要な日は必ずカバーされる。
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(args.start, args.end)]

    total = len(days) * len(universe)
    done_skip, done_fetch, failed = 0, 0, []
    started = time.monotonic()
    first_request = True

    for asof in days:
        for s in universe:
            code, name = s["code"], s.get("news_name", s["name"])
            out_path = NEWS_HISTORY_DIR / code / f"{asof}.json"
            if out_path.exists():
                done_skip += 1
                continue
            if not first_request:
                time.sleep(SLEEP_SEC)
            first_request = False
            try:
                data = fetch_one(name, code, asof)
            except RuntimeError as e:
                print(f"  [NG ] {asof} {code} {name}: {e}", file=sys.stderr)
                failed.append((asof, code))
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            done_fetch += 1
            elapsed = time.monotonic() - started
            print(f"  [OK ] {asof} {code} {name}: {data['count']:3d} 件"
                  f"  ({done_skip + done_fetch + len(failed)}/{total}, {elapsed:.0f}s)")

    elapsed = time.monotonic() - started
    print(f"\n完了: 取得 {done_fetch} / キャッシュ済スキップ {done_skip}"
          f" / 失敗 {len(failed)} (計 {total}) 所要 {elapsed:.0f}s")
    if failed:
        print(f"失敗分: {failed}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
