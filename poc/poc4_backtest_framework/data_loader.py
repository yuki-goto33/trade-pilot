"""J-Quants 日足 CSV を Backtesting.py 形式の OHLCV DataFrame 群に変換するローダ。

PoC-1 で取得した data/prices_jquants.csv（11 銘柄 × 2 年）を読み込み、
銘柄コードごとに Backtesting.py が要求する列名
(Open/High/Low/Close/Volume, DatetimeIndex) の DataFrame を返す。

調整後株価 (AdjO/AdjH/AdjL/AdjC/AdjVo) を使用する
（株式分割・併合をまたいでも連続した価格系列になるため）。
"""
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = REPO_ROOT / "data" / "prices_jquants.csv"

# J-Quants の調整後 OHLCV 列 -> Backtesting.py の要求列名
_COLUMN_MAP = {
    "AdjO": "Open",
    "AdjH": "High",
    "AdjL": "Low",
    "AdjC": "Close",
    "AdjVo": "Volume",
}


def load_prices(csv_path=DEFAULT_CSV):
    """CSV を読み込み {銘柄コード(str): OHLCV DataFrame} を返す。"""
    df = pd.read_csv(csv_path, dtype={"Code": str}, parse_dates=["Date"])
    out = {}
    for code, g in df.groupby("Code"):
        ohlcv = (
            g.set_index("Date")
            .sort_index()[list(_COLUMN_MAP)]
            .rename(columns=_COLUMN_MAP)
            .dropna(subset=["Open", "High", "Low", "Close"])
        )
        out[code] = ohlcv
    return out


if __name__ == "__main__":
    prices = load_prices()
    for code, ohlcv in sorted(prices.items()):
        print(
            f"{code}: {len(ohlcv):4d} rows  "
            f"{ohlcv.index.min().date()} -> {ohlcv.index.max().date()}  "
            f"close last={ohlcv['Close'].iloc[-1]:,.1f}"
        )
