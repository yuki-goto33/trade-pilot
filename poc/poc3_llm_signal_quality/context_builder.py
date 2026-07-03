"""PoC-3: 銘柄コードから LLM 分析入力（コンテキスト）を組み立てる。

data/ にある PoC-1 の取得データを読み、1銘柄分の分析入力 dict を生成する。
dict はそのまま JSON 化してユーザープロンプトに埋め込む中間表現。

入力データ（すべて poc1 のスクリプトが data/ に出力したもの）:
- prices_yfinance.csv     : 日足 OHLCV 直近30日 → テクニカル指標を計算
- news_google.json        : 銘柄別 Google News 見出し（直近分）
- disclosures_yanoshin.json: 銘柄別 適時開示タイトル
- macro_yfinance.csv      : 日経平均/TOPIX ETF/ドル円/S&P500/VIX（直近5日）
- macro_jgb.json          : 日本10年金利
- macro_fred.json         : 米10年金利・FF金利

テクニカル指標は pandas のみで計算する（talib 等の追加依存は使わない）:
- SMA5 / SMA25
- RSI(14)（Wilder 平滑）
- MACD(12, 26, 9)
- 直近5営業日の値動きサマリー

単体実行: `python context_builder.py 7203` で 1銘柄分のコンテキスト JSON を表示。
"""
import json
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd

POC_DIR = Path(__file__).resolve().parent
REPO_ROOT = POC_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data"

# poc1 の universe 定義を共有する
sys.path.insert(0, str(POC_DIR.parent / "poc1_data_sources"))
from universe import UNIVERSE  # noqa: E402

# コンテキストに含める件数の上限（プロンプトのトークン量を抑える）
MAX_NEWS_ITEMS = 10
MAX_DISCLOSURE_ITEMS = 10

# マクロスナップショットに含める市場系列（macro_yfinance.csv の ticker）
MACRO_TICKERS = ["^N225", "1306.T", "JPY=X", "^GSPC", "^VIX"]


class ContextBuildError(Exception):
    """必須データが欠けている等でコンテキストを組み立てられない場合の例外。"""


def _load_json(name: str) -> dict:
    path = DATA_DIR / name
    if not path.exists():
        raise ContextBuildError(
            f"{path} がありません。poc1 の fetch スクリプトを先に実行してください。"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _load_prices() -> pd.DataFrame:
    path = DATA_DIR / "prices_yfinance.csv"
    if not path.exists():
        raise ContextBuildError(
            f"{path} がありません。poc1/fetch_prices_yfinance.py を先に実行してください。"
        )
    df = pd.read_csv(path, parse_dates=["Date"])
    return df.sort_values("Date")


@lru_cache(maxsize=1)
def _load_macro_prices() -> pd.DataFrame:
    path = DATA_DIR / "macro_yfinance.csv"
    if not path.exists():
        raise ContextBuildError(
            f"{path} がありません。poc1/fetch_macro.py を先に実行してください。"
        )
    df = pd.read_csv(path, parse_dates=["Date"])
    return df.sort_values("Date")


def _stock_meta(code: str) -> dict:
    for s in UNIVERSE:
        if s["code"] == code:
            return s
    raise ContextBuildError(f"銘柄コード {code} は universe に定義されていません。")


def _round(v, ndigits=2):
    """NaN は None（JSON では null）に落とし、それ以外は丸める。"""
    if v is None or pd.isna(v):
        return None
    return round(float(v), ndigits)


# ---------------------------------------------------------------------------
# テクニカル指標
# ---------------------------------------------------------------------------

def _rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI（Wilder 平滑）。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def build_technical(code: str) -> dict:
    """日足30日から SMA/RSI/MACD と直近5日の値動きサマリーを計算する。"""
    df = _load_prices()
    ticker = f"{code}.T"
    px = df[df["ticker"] == ticker].copy()
    if px.empty:
        raise ContextBuildError(f"prices_yfinance.csv に {ticker} のデータがありません。")

    close = px["Close"].reset_index(drop=True)
    px = px.reset_index(drop=True)

    px["sma5"] = close.rolling(5).mean()
    px["sma25"] = close.rolling(25).mean()
    px["rsi14"] = _rsi_wilder(close, 14)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    px["macd"] = ema12 - ema26
    px["macd_signal"] = px["macd"].ewm(span=9, adjust=False).mean()
    px["macd_hist"] = px["macd"] - px["macd_signal"]
    px["change_pct"] = close.pct_change() * 100

    last = px.iloc[-1]

    recent5 = [
        {
            "date": row["Date"].strftime("%Y-%m-%d"),
            "close": _round(row["Close"]),
            "change_pct": _round(row["change_pct"]),
            "volume": int(row["Volume"]),
        }
        for _, row in px.tail(5).iterrows()
    ]

    period_high = px["High"].max()
    period_low = px["Low"].min()

    return {
        "as_of": last["Date"].strftime("%Y-%m-%d"),
        "lookback_days": int(len(px)),
        "note": "日足・直近約30営業日分から計算。MACD は約30本での近似値。",
        "last_close": _round(last["Close"]),
        "period_high": _round(period_high),
        "period_low": _round(period_low),
        "sma5": _round(last["sma5"]),
        "sma25": _round(last["sma25"]),
        "sma5_vs_sma25": (
            "golden(5日線が25日線の上)"
            if pd.notna(last["sma5"]) and pd.notna(last["sma25"]) and last["sma5"] > last["sma25"]
            else "dead(5日線が25日線の下)"
            if pd.notna(last["sma5"]) and pd.notna(last["sma25"])
            else None
        ),
        "rsi14": _round(last["rsi14"], 1),
        "macd": _round(last["macd"]),
        "macd_signal": _round(last["macd_signal"]),
        "macd_histogram": _round(last["macd_hist"]),
        "recent_5_days": recent5,
    }


# ---------------------------------------------------------------------------
# ニュース・開示
# ---------------------------------------------------------------------------

def build_news(code: str) -> dict:
    """Google News の銘柄別見出し（直近分）。"""
    data = _load_json("news_google.json")
    entry = (data.get("news") or {}).get(code)
    items = (entry or {}).get("items", [])[:MAX_NEWS_ITEMS]
    return {
        "source": "Google News RSS（直近1日）",
        "fetched_at": data.get("fetched_at"),
        "total_count": (entry or {}).get("count", 0),
        "headlines": [
            {
                "title": it.get("title"),
                "published": it.get("published"),
                "publisher": it.get("source"),
            }
            for it in items
        ],
    }


def build_disclosures(code: str) -> dict:
    """yanoshin TDnet の直近適時開示タイトル。"""
    data = _load_json("disclosures_yanoshin.json")
    entry = (data.get("disclosures") or {}).get(code)
    items = (entry or {}).get("items", [])[:MAX_DISCLOSURE_ITEMS]
    return {
        "source": "TDnet 適時開示（yanoshin API）",
        "fetched_at": data.get("fetched_at"),
        "items": [
            {"date": it.get("pubdate"), "title": it.get("title")}
            for it in items
        ],
    }


# ---------------------------------------------------------------------------
# マクロ
# ---------------------------------------------------------------------------

def build_macro() -> dict:
    """指数・為替・金利のスナップショット（銘柄に依らず共通）。"""
    df = _load_macro_prices()

    indices = []
    for ticker in MACRO_TICKERS:
        series = df[df["ticker"] == ticker]
        if series.empty:
            continue
        label = series["label"].iloc[-1]
        last = series.iloc[-1]
        first = series.iloc[0]
        prev_close = series["Close"].iloc[-2] if len(series) >= 2 else None
        change_1d = (
            (last["Close"] / prev_close - 1) * 100 if prev_close else None
        )
        change_period = (last["Close"] / first["Close"] - 1) * 100
        indices.append(
            {
                "name": label,
                "ticker": ticker,
                "date": last["Date"].strftime("%Y-%m-%d"),
                "close": _round(last["Close"]),
                "change_1d_pct": _round(change_1d),
                "change_5d_pct": _round(change_period),
            }
        )

    rates = {}
    try:
        jgb = _load_json("macro_jgb.json")
        rates["jgb_10y_percent"] = {
            "value": jgb.get("jgb_10y_percent"),
            "date": jgb.get("date"),
        }
    except ContextBuildError:
        rates["jgb_10y_percent"] = None

    try:
        fred = _load_json("macro_fred.json")
        for series_id, info in (fred.get("series") or {}).items():
            obs = info.get("observations") or []
            if obs:
                rates[series_id] = {
                    "label": info.get("label"),
                    "value": float(obs[0]["value"]),
                    "date": obs[0]["date"],
                }
    except ContextBuildError:
        pass

    return {"indices": indices, "rates": rates}


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def build_context(code: str) -> dict:
    """1銘柄分の分析入力コンテキストを組み立てる。

    Returns:
        LLM プロンプトに埋め込む中間表現（JSON 化可能な dict）。
    """
    meta = _stock_meta(code)
    return {
        "meta": {
            "code": code,
            "name": meta["name"],
            "ticker": f"{code}.T",
        },
        "price_technical": build_technical(code),
        "news": build_news(code),
        "disclosures": build_disclosures(code),
        "macro": build_macro(),
    }


def context_to_json(context: dict) -> str:
    """プロンプト埋め込み用の JSON 文字列（日本語をエスケープしない）。"""
    return json.dumps(context, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else UNIVERSE[0]["code"]
    print(context_to_json(build_context(target)))
