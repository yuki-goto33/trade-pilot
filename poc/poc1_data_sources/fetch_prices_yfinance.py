"""yfinance でユニバース銘柄の日足 OHLCV（直近150日 + 調整後終値）を取得する。

v5: 60営業日騰落・対TOPIX相対騰落（テクニカル専門家の中期トレンド入力）の
計算に61営業日以上が必要なため、取得期間を 30d → 150d に拡張。
テクニカル指標（SMA/RSI/MACD）は従来どおり末尾30営業日窓で計算される。

- ティッカーは <4桁コード>.T 形式
- まずバッチ取得（threads=False で内部逐次）、失敗銘柄は1req/秒で個別リトライ
- 直近バーが NaN になる事象があるため dropna で除去
- 出力: data/prices_yfinance.csv（long 形式）
"""
import sys
import time

import pandas as pd
import yfinance as yf

from common import Timer, ensure_data_dir, now_jst_iso, parse_universe_arg, print_summary, DATA_DIR
from universe import load_universe, yf_tickers

SOURCE = "prices_yfinance"
PERIOD = "150d"


def _to_long(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """1銘柄分の OHLCV DataFrame を long 形式に整形する。"""
    df = df.dropna(how="any")
    df = df.reset_index()
    df["ticker"] = ticker
    return df


def fetch(universe) -> pd.DataFrame:
    tickers = yf_tickers(universe)
    frames = []
    failed = []

    # バッチ取得（threads=False で逐次リクエスト）
    try:
        raw = yf.download(
            tickers=tickers,
            period=PERIOD,
            interval="1d",
            auto_adjust=False,  # Adj Close 列を保持
            group_by="ticker",
            threads=False,
            progress=False,
        )
    except Exception as e:
        print(f"  batch download failed: {e}", file=sys.stderr)
        raw = None

    if raw is not None and not raw.empty:
        for t in tickers:
            try:
                sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                sub = sub.dropna(how="all")
                if sub.empty:
                    failed.append(t)
                    continue
                frames.append(_to_long(sub, t))
            except KeyError:
                failed.append(t)
    else:
        failed = list(tickers)

    # 失敗銘柄を 1req/秒 で個別リトライ（429 対策）
    for t in failed[:]:
        time.sleep(1.0)
        try:
            sub = yf.download(
                t, period=PERIOD, interval="1d",
                auto_adjust=False, threads=False, progress=False,
            )
            if isinstance(sub.columns, pd.MultiIndex):
                sub.columns = sub.columns.get_level_values(0)
            sub = sub.dropna(how="all")
            if not sub.empty:
                frames.append(_to_long(sub, t))
                failed.remove(t)
        except Exception as e:
            print(f"  retry failed for {t}: {e}", file=sys.stderr)

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
            out = DATA_DIR / "prices_yfinance.csv"
            df.to_csv(out, index=False)
            summary = {
                "source": SOURCE, "ok": True, "count": len(df),
                "note": f"{df['ticker'].nunique()}/{len(universe)} 銘柄, 保存先 {out.name}",
            }
        except Exception as e:
            summary = {"source": SOURCE, "ok": False, "count": 0, "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main(parse_universe_arg(__doc__))["ok"] else 1)
