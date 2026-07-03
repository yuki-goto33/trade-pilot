"""yanoshin TDnet WEB-API でユニバース銘柄の直近適時開示一覧を取得する。

- エンドポイント: https://webapi.yanoshin.jp/webapi/tdnet/list/<4桁コード>.json
- 個人運営の非公式 API のためリクエスト間 3 秒以上あける
- 出力: data/disclosures_yanoshin.json
"""
import sys
import time

import requests

from common import Timer, now_jst_iso, print_summary, save_json
from universe import UNIVERSE

SOURCE = "disclosures_yanoshin"
BASE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list"
SLEEP_SEC = 3.5
UA = "trade-pilot-poc1 (personal research)"


def fetch() -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    results = {}
    failed = []
    for i, s in enumerate(UNIVERSE):
        if i > 0:
            time.sleep(SLEEP_SEC)
        code = s["code"]
        try:
            r = session.get(f"{BASE_URL}/{code}.json", params={"limit": 50}, timeout=30)
            r.raise_for_status()
            payload = r.json()
            items = payload.get("items", [])
            # {"Tdnet": {...}} のラップを外す
            docs = [it.get("Tdnet", it) for it in items]
            results[code] = {"name": s["name"], "count": len(docs), "items": docs}
            print(f"  {code} {s['name']}: {len(docs)} 件")
        except Exception as e:
            print(f"  {code} 失敗: {e}", file=sys.stderr)
            failed.append(code)

    if not results:
        raise RuntimeError("全銘柄の取得に失敗")
    return {"fetched_at": now_jst_iso(), "failed": failed, "disclosures": results}


def main() -> dict:
    with Timer() as t:
        try:
            data = fetch()
            out = save_json("disclosures_yanoshin.json", data)
            count = sum(v["count"] for v in data["disclosures"].values())
            note = f"{len(data['disclosures'])}/{len(UNIVERSE)} 銘柄, 保存先 {out.name}"
            summary = {"source": SOURCE, "ok": True, "count": count, "note": note}
        except Exception as e:
            summary = {"source": SOURCE, "ok": False, "count": 0, "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
