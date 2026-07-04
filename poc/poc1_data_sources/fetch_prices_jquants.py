"""J-Quants API V2（無料プラン）でユニバース銘柄の過去2年日足を取得する。

- 認証: x-api-key ヘッダー（.env の JQUANTS_API_KEY）
- エンドポイント: GET /v2/equities/bars/daily?code=<5桁>&from=YYYYMMDD&to=YYYYMMDD
  （5桁コード = 4桁コード + "0"。例: 7203 → 72030）
- レート制限 5req/分（ローリング60秒窓）→ リクエスト間 15.5 秒
  （13 秒間隔では任意の60秒窓に5リクエスト入り 429 になる【実測】）
- 無料プランは 12 週間遅延: 購読範囲は「(今日-12週-2年) 〜 (今日-12週)」で、
  範囲外の日付を指定すると 400 が返る【実測】→ from/to を範囲内にクランプする。
  400 時はエラーメッセージ中の購読範囲をパースして1回だけリトライする。
- 出力: data/prices_jquants.csv + data/prices_jquants_meta.json
"""
import os
import re
import sys
import time
from datetime import date, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

from common import Timer, ensure_data_dir, now_jst_iso, parse_universe_arg, print_summary, save_json, DATA_DIR, REPO_ROOT
from universe import load_universe

SOURCE = "prices_jquants"
BASE_URL = "https://api.jquants.com/v2"
SLEEP_SEC = 15.5  # 5req/分（ローリング窓）制限対策
DELAY_WEEKS = 12  # 無料プランの遅延
HISTORY_DAYS = 365 * 2  # 無料プランの履歴


def _extract_rows(payload: dict):
    """レスポンス JSON から明細リストを取り出す（キー名の揺れに対応）。"""
    for key in ("daily_quotes", "bars", "data"):
        if isinstance(payload.get(key), list):
            return payload[key]
    for v in payload.values():
        if isinstance(v, list):
            return v
    return []


def _covered_range_from_error(resp) -> "tuple | None":
    """400 エラーの購読範囲メッセージから (from, to) を YYYYMMDD で抽出する。"""
    try:
        msg = resp.json().get("message", "")
    except Exception:
        return None
    dates = re.findall(r"(\d{4})-(\d{2})-(\d{2})", msg)
    if len(dates) >= 2:
        return "".join(dates[0]), "".join(dates[1])
    return None


def fetch_one(session: requests.Session, code5: str, d_from: str, d_to: str) -> list:
    rows = []
    params = {"code": code5, "from": d_from, "to": d_to}
    retried = False
    while True:
        r = session.get(f"{BASE_URL}/equities/bars/daily", params=params, timeout=30)
        if r.status_code == 400 and not retried:
            # 購読範囲外 → エラーメッセージの範囲にクランプして1回リトライ
            covered = _covered_range_from_error(r)
            if covered:
                params["from"] = max(params["from"], covered[0])
                params["to"] = min(params["to"], covered[1])
                retried = True
                time.sleep(SLEEP_SEC)
                continue
        r.raise_for_status()
        payload = r.json()
        rows.extend(_extract_rows(payload))
        pk = payload.get("pagination_key")
        if not pk:
            break
        params["pagination_key"] = pk
        time.sleep(SLEEP_SEC)
    return rows


def fetch(universe) -> pd.DataFrame:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("JQUANTS_API_KEY")
    if not api_key:
        raise RuntimeError("JQUANTS_API_KEY が .env に設定されていません")

    # 無料プランの購読範囲: (今日 - 12週 - 2年) 〜 (今日 - 12週)
    d_to_date = date.today() - timedelta(weeks=DELAY_WEEKS)
    d_from = (d_to_date - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    d_to = d_to_date.strftime("%Y%m%d")

    session = requests.Session()
    session.headers.update({"x-api-key": api_key})

    frames = []
    failed = []
    for i, s in enumerate(universe):
        code5 = s["code"] + "0"
        if i > 0:
            time.sleep(SLEEP_SEC)
        try:
            rows = fetch_one(session, code5, d_from, d_to)
            print(f"  {s['code']} {s['name']}: {len(rows)} 行")
            if rows:
                df = pd.DataFrame(rows)
                frames.append(df)
            else:
                failed.append(s["code"])
        except Exception as e:
            print(f"  {s['code']} 失敗: {e}", file=sys.stderr)
            failed.append(s["code"])

    if failed:
        print(f"  取得できなかった銘柄: {failed}", file=sys.stderr)
    if not frames:
        raise RuntimeError("全銘柄の取得に失敗")
    return pd.concat(frames, ignore_index=True)


def main(universe_path=None) -> dict:
    universe = load_universe(universe_path)
    with Timer() as t:
        try:
            df = fetch(universe)
            ensure_data_dir()
            out = DATA_DIR / "prices_jquants.csv"
            df.to_csv(out, index=False)
            date_col = next((c for c in df.columns if c.lower() == "date"), None)
            note = f"保存先 {out.name}"
            if date_col:
                note += f", 期間 {df[date_col].min()}〜{df[date_col].max()}"
            save_json("prices_jquants_meta.json", {
                "fetched_at": now_jst_iso(), "rows": len(df), "columns": list(df.columns),
            })
            summary = {"source": SOURCE, "ok": True, "count": len(df), "note": note}
        except Exception as e:
            summary = {"source": SOURCE, "ok": False, "count": 0, "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main(parse_universe_arg(__doc__))["ok"] else 1)
