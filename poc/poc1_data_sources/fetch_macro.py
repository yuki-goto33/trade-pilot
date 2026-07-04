"""マクロ指標を取得する。

1. yfinance: ^N225, 1306.T(TOPIX連動ETF), JPY=X, ^GSPC, ^VIX
   + TOPIX-17 業種 ETF（1617.T〜1633.T）の直近約60日
   （v2: 市場レジーム判定に TOPIX の 25日線が必要なため 5d → 60d に拡張。
     poc3 context_builder のスナップショットは末尾5日窓を使うので互換）
2. 財務省 国債金利 CSV（jgbcm.csv, cp932, 和暦）から直近の10年金利を抽出
3. FRED API で DGS10（米10年債利回り）と DFF（FF金利）の直近5観測値を取得
   （.env の FRED_API_KEY を使用）

出力: data/macro_yfinance.csv + data/macro_jgb.json + data/macro_fred.json
"""
import io
import os
import re
import sys
import time

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

from common import Timer, ensure_data_dir, now_jst_iso, print_summary, save_json, DATA_DIR, REPO_ROOT

SOURCE = "macro"

INDEX_TICKERS = {
    "^N225": "日経平均",
    "1306.T": "TOPIX連動ETF",
    "JPY=X": "ドル円",
    "^GSPC": "S&P500",
    "^VIX": "VIX",
}
SECTOR_ETFS = {f"{code}.T": f"TOPIX-17 ETF {code}" for code in range(1617, 1634)}

JGB_CSV_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES = {
    "DGS10": "米10年債利回り",
    "DFF": "FF金利（実効）",
}

ERA_BASE = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}


def wareki_to_date(s: str):
    """'R7.7.3' のような和暦表記を datetime.date に変換する。"""
    m = re.match(r"^([MTSHR])(\d+)\.(\d+)\.(\d+)$", str(s).strip())
    if not m:
        return None
    era, y, mo, d = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return pd.Timestamp(ERA_BASE[era] + y, mo, d).date()


def fetch_yfinance() -> pd.DataFrame:
    tickers = {**INDEX_TICKERS, **SECTOR_ETFS}
    raw = yf.download(
        tickers=list(tickers),
        period="60d",
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        threads=False,
        progress=False,
    )
    frames = []
    failed = []
    for t, label in tickers.items():
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            sub = sub.dropna(how="any")  # 直近バーの NaN 対策
            if sub.empty:
                failed.append(t)
                continue
            sub = sub.reset_index()
            sub["ticker"] = t
            sub["label"] = label
            frames.append(sub)
        except KeyError:
            failed.append(t)
    if failed:
        print(f"  yfinance 取得できなかったティッカー: {failed}", file=sys.stderr)
    if not frames:
        raise RuntimeError("yfinance マクロ系列の取得に全滅")
    return pd.concat(frames, ignore_index=True)


def fetch_jgb() -> dict:
    r = requests.get(JGB_CSV_URL, timeout=30)
    r.raise_for_status()
    text = r.content.decode("cp932")
    df = pd.read_csv(io.StringIO(text), skiprows=1)
    df.columns = [str(c).strip() for c in df.columns]
    date_col = df.columns[0]
    df["date"] = df[date_col].map(wareki_to_date)
    df = df.dropna(subset=["date"])

    col_10y = next(c for c in df.columns if "10" in c and "年" in c)
    df["y10"] = pd.to_numeric(df[col_10y], errors="coerce")
    latest = df.dropna(subset=["y10"]).iloc[-1]
    return {
        "source": JGB_CSV_URL,
        "date": str(latest["date"]),
        "jgb_10y_percent": float(latest["y10"]),
        "rows_in_csv": len(df),
    }


def fetch_fred() -> dict:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY が .env に設定されていません")
    out = {}
    for i, (series_id, label) in enumerate(FRED_SERIES.items()):
        if i > 0:
            time.sleep(1.0)
        r = requests.get(FRED_URL, params={
            "series_id": series_id, "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": 5,
        }, timeout=30)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        out[series_id] = {
            "label": label,
            "observations": [{"date": o["date"], "value": o["value"]} for o in obs],
        }
    return out


def main() -> dict:
    with Timer() as t:
        notes = []
        count = 0
        ok_any = False
        try:
            df = fetch_yfinance()
            ensure_data_dir()
            out = DATA_DIR / "macro_yfinance.csv"
            df.to_csv(out, index=False)
            count += len(df)
            ok_any = True
            notes.append(f"yfinance {df['ticker'].nunique()} 系列 {len(df)} 行")
        except Exception as e:
            notes.append(f"yfinance 失敗: {type(e).__name__}: {e}")
        try:
            jgb = fetch_jgb()
            jgb["fetched_at"] = now_jst_iso()
            save_json("macro_jgb.json", jgb)
            count += 1
            ok_any = True
            notes.append(f"JGB10年 {jgb['jgb_10y_percent']}% ({jgb['date']})")
        except Exception as e:
            notes.append(f"JGB 失敗: {type(e).__name__}: {e}")
        try:
            fred = fetch_fred()
            n_obs = sum(len(v["observations"]) for v in fred.values())
            save_json("macro_fred.json", {"fetched_at": now_jst_iso(), "series": fred})
            count += n_obs
            ok_any = True
            latest = {
                k: next((o["value"] for o in v["observations"] if o["value"] != "."), None)
                for k, v in fred.items()
            }
            notes.append(f"FRED {n_obs} 観測 (DGS10={latest.get('DGS10')}, DFF={latest.get('DFF')})")
        except Exception as e:
            notes.append(f"FRED 失敗: {type(e).__name__}: {e}")
        summary = {"source": SOURCE, "ok": ok_any, "count": count, "note": " / ".join(notes)}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
