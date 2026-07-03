"""EDINET API v2 でユニバース銘柄の直近30日の提出書類を取得する。

- 認証: クエリパラメータ `Subscription-Key=<EDINET_API_KEY>`（.env から読む）
- 書類一覧: GET /api/v2/documents.json?date=YYYY-MM-DD&type=2（メタデータ込み一覧）
  を直近30日分（1日1リクエスト、3.5秒間隔）取得し、universe の secCode
  （証券コード4桁 + "0" の5桁。英字入りコードもそのまま連結: 285A → 285A0）
  に一致する書類を抽出する。
- 該当書類が1件でもあれば、csvFlag=1 の書類を1件だけ type=5（CSV ZIP）で
  実ダウンロードし、解凍して pandas で読めることを確認する
  （EDINET の CSV は UTF-16LE・タブ区切り）。
- 出力: data/financials_edinet.json + data/financials_edinet_sample.csv
"""
import io
import os
import sys
import time
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

from common import Timer, ensure_data_dir, now_jst_iso, print_summary, save_json, DATA_DIR, REPO_ROOT
from universe import UNIVERSE, edinet_sec_codes

SOURCE = "financials_edinet"
BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
SLEEP_SEC = 3.5  # 実測で 1req 3〜5 秒間隔が必要
LOOKBACK_DAYS = 30


def _doc_to_dict(d: dict) -> dict:
    keys = (
        "docID", "secCode", "filerName", "docTypeCode", "docDescription",
        "submitDateTime", "periodStart", "periodEnd", "csvFlag", "xbrlFlag",
    )
    return {k: d.get(k) for k in keys}


def fetch_list(api_key: str) -> list:
    sec_codes = set(edinet_sec_codes())
    matched = []
    today = date.today()
    for i in range(LOOKBACK_DAYS):
        d = today - timedelta(days=i)
        if i > 0:
            time.sleep(SLEEP_SEC)
        try:
            r = requests.get(
                f"{BASE_URL}/documents.json",
                params={"date": d.isoformat(), "type": 2, "Subscription-Key": api_key},
                timeout=30,
            )
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception as e:
            print(f"  {d} 一覧取得失敗: {e}", file=sys.stderr)
            continue
        hits = [_doc_to_dict(doc) for doc in results if doc.get("secCode") in sec_codes]
        if hits:
            print(f"  {d}: {len(hits)} 件 ({', '.join(h['filerName'] or '' for h in hits)})")
        matched.extend(hits)
    return matched


def download_sample_csv(api_key: str, doc: dict) -> dict:
    """type=5 で CSV ZIP を1件ダウンロードし、pandas で読めることを確認する。"""
    time.sleep(SLEEP_SEC)
    r = requests.get(
        f"{BASE_URL}/documents/{doc['docID']}",
        params={"type": 5, "Subscription-Key": api_key},
        timeout=60,
    )
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not csv_names:
        raise RuntimeError(f"ZIP 内に CSV がない: {zf.namelist()[:5]}")
    with zf.open(csv_names[0]) as f:
        df = pd.read_csv(f, encoding="utf-16", sep="\t")
    ensure_data_dir()
    out = DATA_DIR / "financials_edinet_sample.csv"
    df.to_csv(out, index=False)
    return {
        "docID": doc["docID"], "filerName": doc["filerName"],
        "docDescription": doc["docDescription"], "zip_csv_count": len(csv_names),
        "sample_csv": csv_names[0], "sample_rows": len(df),
        "sample_columns": list(df.columns)[:10], "saved_to": out.name,
    }


def main() -> dict:
    with Timer() as t:
        try:
            load_dotenv(REPO_ROOT / ".env")
            api_key = os.getenv("EDINET_API_KEY")
            if not api_key:
                raise RuntimeError("EDINET_API_KEY が .env に設定されていません")

            matched = fetch_list(api_key)
            sample = None
            note_parts = [f"{len(matched)} 書類 / 直近{LOOKBACK_DAYS}日"]
            csv_docs = [d for d in matched if str(d.get("csvFlag")) == "1"]
            if csv_docs:
                try:
                    sample = download_sample_csv(api_key, csv_docs[0])
                    note_parts.append(
                        f"CSV検証OK: {sample['filerName']} {sample['sample_rows']}行"
                    )
                except Exception as e:
                    note_parts.append(f"CSV検証失敗: {type(e).__name__}: {e}")
            else:
                note_parts.append("csvFlag=1 の書類なし（CSV検証スキップ）")

            save_json("financials_edinet.json", {
                "fetched_at": now_jst_iso(),
                "lookback_days": LOOKBACK_DAYS,
                "universe_sec_codes": edinet_sec_codes(),
                "documents": matched,
                "csv_sample": sample,
            })
            summary = {"source": SOURCE, "ok": True, "count": len(matched),
                       "note": ", ".join(note_parts)}
        except Exception as e:
            summary = {"source": SOURCE, "ok": False, "count": 0, "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
