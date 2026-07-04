"""PoC-3: 銘柄コードから LLM 分析入力（コンテキスト）を組み立てる。

data/ にある PoC-1 の取得データを読み、1銘柄分の分析入力 dict を生成する。
dict はそのまま JSON 化してユーザープロンプトに埋め込む中間表現。

入力データ（すべて poc1 のスクリプトが data/ に出力したもの）:
- prices_yfinance.csv     : 日足 OHLCV 直近30日 → テクニカル指標を計算
- news_google.json        : 銘柄別 Google News 見出し（直近分）
- disclosures_yanoshin.json: 銘柄別 適時開示タイトル
- fundamentals_yfinance.json: バリュエーション・業績トレンド・次回決算日
- disclosure_summaries.json: 重要開示（決算短信等）の Gemini 要約キャッシュ
- macro_yfinance.csv      : 日経平均/TOPIX ETF/ドル円/S&P500/VIX
  （直近約60日。スナップショットは末尾5日窓、市場レジーム判定に全履歴を使用）
- macro_jgb.json          : 日本10年金利
- macro_fred.json         : 米10年金利・FF金利

fundamentals セクションは「データが無いフィールドは項目ごと省略し、note に
欠損を明記する」方針（"N/A" 値をコンテキストに混ぜない）。
fundamentals_yfinance.json 自体が無い場合も例外にせず note のみ返す。

テクニカル指標は pandas のみで計算する（talib 等の追加依存は使わない）:
- SMA5 / SMA25
- RSI(14)（Wilder 平滑）
- MACD(12, 26, 9)
- 直近5営業日の値動きサマリー

単体実行: `python context_builder.py 7203` で 1銘柄分のコンテキスト JSON を表示。
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
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
MAX_DISCLOSURE_SUMMARIES = 2

# 開示要約をコンテキストに含める期間（日）
DISCLOSURE_SUMMARY_RECENT_DAYS = 90

# マクロスナップショットに含める市場系列（macro_yfinance.csv の ticker）
MACRO_TICKERS = ["^N225", "1306.T", "JPY=X", "^GSPC", "^VIX"]

# マクロスナップショットの営業日数（indices の change_5d_pct 用）
MACRO_SNAPSHOT_BDAYS = 5

# 市場レジーム判定に使う系列（TOPIX 連動 ETF）
REGIME_TICKER = "1306.T"


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


def technical_from_window(px: pd.DataFrame) -> dict:
    """日足ウィンドウ（約30営業日、列: Date/Open/High/Low/Close/Volume）から
    SMA/RSI/MACD と直近5日の値動きサマリーを計算する。

    build_technical（最新データ）と historical_context.build_technical_asof
    （過去 as-of 時点）の共通実装。
    """
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


def build_technical(code: str) -> dict:
    """日足30日から SMA/RSI/MACD と直近5日の値動きサマリーを計算する。"""
    df = _load_prices()
    ticker = f"{code}.T"
    px = df[df["ticker"] == ticker].copy()
    if px.empty:
        raise ContextBuildError(f"prices_yfinance.csv に {ticker} のデータがありません。")
    return technical_from_window(px)


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
# ファンダメンタルズ
# ---------------------------------------------------------------------------

def _oku(v):
    """円 → 億円（丸め）。None はそのまま。"""
    if v is None:
        return None
    return round(v / 1e8)


def _fundamentals_period(p: dict) -> dict:
    """業績1期分を億円換算し、欠損フィールドは省略した dict にする。"""
    out = {"period": p.get("period")}
    for src, dst in (
        ("revenue_jpy", "revenue_oku_jpy"),
        ("operating_income_jpy", "operating_income_oku_jpy"),
        ("net_income_jpy", "net_income_oku_jpy"),
    ):
        if p.get(src) is not None:
            out[dst] = _oku(p[src])
    if p.get("diluted_eps") is not None:
        out["diluted_eps"] = p["diluted_eps"]
    return out


def _load_disclosure_summaries(code: str) -> list:
    """disclosure_summaries.json から該当銘柄の直近の要約を取り出す。"""
    try:
        data = _load_json("disclosure_summaries.json")
    except ContextBuildError:
        return []
    cutoff = (datetime.now() - timedelta(days=DISCLOSURE_SUMMARY_RECENT_DAYS)).strftime(
        "%Y-%m-%d"
    )
    items = [
        s for s in (data.get("summaries") or {}).values()
        if s.get("code") == code and s.get("summary")
        and (s.get("pubdate") or "") >= cutoff
    ]
    items.sort(key=lambda s: s["pubdate"], reverse=True)
    return [
        {"date": s["pubdate"][:10], "title": s["title"], "summary": s["summary"]}
        for s in items[:MAX_DISCLOSURE_SUMMARIES]
    ]


def build_fundamentals(code: str) -> dict:
    """バリュエーション・業績トレンド・次回決算日・重要開示の要約。

    データが無いフィールドは "N/A" にせず項目ごと省略し、note に欠損を明記する。
    fundamentals_yfinance.json 自体が無い場合も例外にせず note のみ返す
    （ファンダ欠損でシグナル生成全体を止めない）。
    """
    out = {
        "source": "yfinance（バリュエーション・業績・次回決算日）"
                  "+ TDnet 重要開示の要約（Gemini、直近90日）",
    }
    notes = []

    entry = None
    try:
        data = _load_json("fundamentals_yfinance.json")
        entry = (data.get("fundamentals") or {}).get(code)
    except ContextBuildError:
        notes.append("fundamentals_yfinance.json が未取得")

    if entry is None and not notes:
        notes.append("この銘柄の yfinance ファンダメンタルズが未取得")

    if entry:
        valuation_src = entry.get("valuation") or {}
        valuation = {}
        for key, v in valuation_src.items():
            if v is None:
                continue
            if key == "market_cap_jpy":
                valuation["market_cap_oku_jpy"] = _oku(v)
            else:
                valuation[key] = round(v, 3) if isinstance(v, float) else v
        missing = [k for k, v in valuation_src.items() if v is None]
        if valuation:
            out["valuation"] = valuation
        if missing:
            notes.append(f"バリュエーション欠損: {', '.join(missing)}")

        annual = [_fundamentals_period(p) for p in entry.get("annual") or []]
        quarterly = [_fundamentals_period(p) for p in entry.get("quarterly") or []]
        if annual or quarterly:
            out["earnings_trend"] = {
                "unit_note": "金額は億円（*_oku_jpy）。EPS は円。",
            }
            if annual:
                out["earnings_trend"]["annual"] = annual
            else:
                notes.append("年次業績データなし")
            if quarterly:
                out["earnings_trend"]["quarterly"] = quarterly
            else:
                notes.append("四半期業績データなし（yfinance 未収載）")
        else:
            notes.append("業績トレンドデータなし")

        if entry.get("next_earnings_date"):
            out["next_earnings_date"] = entry["next_earnings_date"]
        else:
            notes.append("次回決算発表日は不明")

    summaries = _load_disclosure_summaries(code)
    if summaries:
        out["recent_important_disclosures"] = summaries
    else:
        notes.append("直近90日の重要開示の本文要約なし（タイトルは disclosures 参照）")

    if notes:
        out["note"] = "欠損: " + " / ".join(notes)
    return out


# ---------------------------------------------------------------------------
# マクロ
# ---------------------------------------------------------------------------

JFC_SME_MONTHS = 6


def jfc_sme_snapshot(asof=None, months: int = JFC_SME_MONTHS) -> "dict | None":
    """日本公庫「中小企業景況調査」の直近 DI スナップショットを組み立てる。

    data/macro_jfc.json（fetch_macro_jfc.py の出力）から直近 months ヶ月分を返す。
    asof を渡すと「調査月の翌月1日以降に利用可能」ルールで look-ahead を防止する
    （例: 6月調査は 6/30 公表のため asof 7/1 以降でのみ含める）。
    データファイルが無い場合は None（呼び出し側でキーごと省略）。

    context_builder.build_macro（最新データ）と
    historical_context.build_macro_asof（過去 as-of 時点）の共通実装。
    """
    try:
        data = _load_json("macro_jfc.json")
    except ContextBuildError:
        return None
    series = data.get("series") or []
    if asof is not None:
        def available(rec):
            y, m = map(int, rec["year_month"].split("-"))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            return date(y, m, 1) <= asof
        series = [r for r in series if available(r)]
    series = series[-months:]
    if not series:
        return None
    return {
        "source": "日本政策金融公庫 中小企業景況調査（月次）",
        "note": (
            "中小企業の景況感 DI（直近{n}ヶ月、DI=良い-悪い の企業割合差）。"
            "販売価格DIと仕入価格DIの差は中小企業の価格転嫁（コスト圧迫）状況を示す。"
            "売上げ・見通し・利益額は季節調整値。"
        ).format(n=len(series)),
        "monthly": series,
    }


def boj_snapshot(asof=None) -> "dict | None":
    """日銀関連スナップショット（決定会合日程・短観 DI・重要発表）を組み立てる。

    data/macro_boj.json（fetch_macro_boj.py の出力）から生成する。
    - 決定会合日程は事前公表のため look-ahead 制約なし（次回/前回を asof 基準で選ぶ）
    - 短観は available_from（公表日翌日）<= asof のもののみ（look-ahead 防止）
    - 新着発表（RSS）は遡及不可のためフォワード（asof=None）でのみ含める

    context_builder.build_macro（最新データ）と
    historical_context.build_macro_asof（過去 as-of 時点）の共通実装。
    """
    try:
        data = _load_json("macro_boj.json")
    except ContextBuildError:
        return None
    ref = asof or datetime.now(timezone(timedelta(hours=9))).date()
    out = {}

    meetings = (data.get("mpm_schedule") or {}).get("meetings") or []
    nxt = next((m for m in meetings if m["start"] >= ref.isoformat()), None)
    last = next((m for m in reversed(meetings) if m["end"] < ref.isoformat()), None)
    if nxt or last:
        mpm = {
            "note": ("日銀の金融政策決定会合（政策金利等を決定）。想定保有期間内に"
                     "次回会合が含まれる場合は金利イベントリスクとして扱うこと"),
        }
        if nxt:
            mpm["next_meeting"] = f"{nxt['start']}〜{nxt['end']}"
            mpm["days_until_next"] = (date.fromisoformat(nxt["start"]) - ref).days
        if last:
            mpm["last_meeting_end"] = last["end"]
        out["monetary_policy_meeting"] = mpm

    surveys = (data.get("tankan") or {}).get("surveys") or []
    avail = [s for s in surveys if s["available_from"] <= ref.isoformat()]
    if avail:
        latest = avail[-1]
        out["tankan"] = {
            "survey": latest["survey"],
            "published": latest["published"],
            "di": latest["di"],
            "note": ("日銀短観の業況判断DI（良い-悪い、%ポイント）。large=大企業/"
                     "small=中小企業 × 製造業/非製造業/全産業。current_di=最近、"
                     "change_from_prev=前回調査比、forecast_di=先行き"),
        }

    items = (data.get("announcements") or {}).get("items") or []
    if asof is None and items:
        out["recent_announcements"] = {
            "note": "日銀の直近の重要発表タイトル（What's New RSS より）",
            "items": [{"published": i["published"], "title": i["title"]}
                      for i in items],
        }
    return out or None


def market_regime_from_series(close: pd.Series) -> dict:
    """TOPIX 連動 ETF (1306.T) の終値系列（昇順）から市場レジームを機械判定する。

    判定ロジック（v2、シンプルに固定）:
    - 終値 > 25日線 かつ 直近5日騰落率 > 0 → "uptrend"
    - 終値 < 25日線 かつ 直近5日騰落率 < 0 → "downtrend"
    - それ以外 → "neutral"

    context_builder.build_macro（最新データ）と
    historical_context.build_macro_asof（過去 as-of 時点）の共通実装。
    データが 25日線計算に足りない場合は neutral + note を返す。
    """
    close = close.dropna()
    if len(close) < 26:
        return {
            "market_regime": "neutral",
            "note": f"TOPIX 終値履歴が25日線計算に不足（{len(close)}本 < 26本）のため neutral 扱い",
        }
    last = float(close.iloc[-1])
    sma25 = float(close.rolling(25).mean().iloc[-1])
    ret5 = (last / float(close.iloc[-6]) - 1) * 100
    ret25 = (last / float(close.iloc[-26]) - 1) * 100
    if last > sma25 and ret5 > 0:
        regime = "uptrend"
    elif last < sma25 and ret5 < 0:
        regime = "downtrend"
    else:
        regime = "neutral"
    return {
        "market_regime": regime,
        "topix_close": _round(last),
        "topix_sma25": _round(sma25),
        "topix_close_vs_sma25": "above" if last >= sma25 else "below",
        "topix_return_5d_pct": _round(ret5),
        "topix_return_25d_pct": _round(ret25),
        "note": "TOPIX連動ETF(1306.T) の終値 vs 25日線と直近5日騰落率による機械判定"
                "（終値>25日線かつ5日騰落>0=uptrend / 終値<25日線かつ5日騰落<0=downtrend"
                " / それ以外=neutral）",
    }


def build_macro() -> dict:
    """指数・為替・金利のスナップショット + 市場レジーム（銘柄に依らず共通）。"""
    df = _load_macro_prices()

    indices = []
    for ticker in MACRO_TICKERS:
        series = df[df["ticker"] == ticker].dropna(subset=["Close"])
        if series.empty:
            continue
        label = series["label"].iloc[-1]
        # スナップショットは直近5営業日窓（CSV に長い履歴があっても 5日騰落を保つ）
        series = series.tail(MACRO_SNAPSHOT_BDAYS)
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

    # 市場レジーム（TOPIX 25日線には CSV 内の全履歴を使う）
    topix = df[df["ticker"] == REGIME_TICKER].dropna(subset=["Close"])
    regime = market_regime_from_series(topix.set_index("Date")["Close"])

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

    macro = {"indices": indices, "market_regime": regime, "rates": rates}
    sme = jfc_sme_snapshot()
    if sme:
        macro["jfc_sme_survey"] = sme
    boj = boj_snapshot()
    if boj:
        macro["boj"] = boj
    return macro


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
        "fundamentals": build_fundamentals(code),
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
