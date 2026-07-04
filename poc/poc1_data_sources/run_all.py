"""PoC-1 全データ取得スクリプトを順に実行し、成否一覧を出力する。

- 各ソースの失敗はスキップして継続する
- 出力: stdout の一覧表 + data/run_summary.json
"""
import sys
import time

import fetch_prices_yfinance
import fetch_prices_jquants
import fetch_financials_edinet
import fetch_disclosures_yanoshin
import fetch_news_google
import fetch_news_macro_rss
import fetch_macro
import fetch_macro_jfc
import fetch_macro_boj
import fetch_macro_pmi
import fetch_macro_cpi
from common import now_jst_iso, save_json

STEPS = [
    fetch_prices_yfinance,
    fetch_prices_jquants,
    fetch_financials_edinet,
    fetch_disclosures_yanoshin,
    fetch_news_google,
    fetch_news_macro_rss,
    fetch_macro,
    fetch_macro_jfc,
    fetch_macro_boj,
    fetch_macro_pmi,
    fetch_macro_cpi,
]

SLEEP_BETWEEN = 3.0


def main() -> int:
    summaries = []
    for i, mod in enumerate(STEPS):
        if i > 0:
            time.sleep(SLEEP_BETWEEN)
        print(f"\n=== {mod.__name__} ===")
        try:
            summaries.append(mod.main())
        except Exception as e:  # main() 内で捕捉できなかった想定外エラー
            summaries.append({
                "source": mod.__name__, "ok": False, "count": 0,
                "seconds": 0, "note": f"{type(e).__name__}: {e}",
                "fetched_at": now_jst_iso(),
            })

    print("\n" + "=" * 72)
    print(f"{'source':<24} {'result':<7} {'count':>7} {'seconds':>8}")
    print("-" * 72)
    for s in summaries:
        result = "OK" if s["ok"] else "FAIL"
        print(f"{s['source']:<24} {result:<7} {s['count']:>7} {s['seconds']:>8}")
    print("=" * 72)

    out = save_json("run_summary.json", {"run_at": now_jst_iso(), "results": summaries})
    print(f"サマリー保存: {out}")
    return 0 if all(s["ok"] for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
