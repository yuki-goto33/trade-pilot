"""ヒストリカル用: 過去の任意営業日 as-of のシグナル生成コンテキストを組み立てる。

build_context_asof(code, date) は「date の朝に入手可能だった情報のみ」で
既存 context_builder.build_context() と同一構造のコンテキストを返す
（プロンプトテンプレートをそのまま使えるようにキー名を揃える）。

各セクションのデータソースと as-of 制約:
- price_technical: data/prices_history.csv（yfinance 日足キャッシュ）の
  date 以前 30 営業日窓。指標計算は context_builder.technical_from_window を再利用。
  ※ date 当日の足は含めない（シグナルは date の朝に生成する想定のため、
    最終足は date の前営業日）。
- news: data/news_history/<code>/<date>.json（fetch_news_range.py のキャッシュ、
  date-3〜date-1 の見出し）から最大 10 件。
- disclosures: data/disclosures_yanoshin.json から date 以前 90 日・最大 10 件。
- fundamentals: fundamentals_yfinance.json の年次/四半期のうち
  「公表時期が date 以前と推定されるもの」のみ。公表日は yanoshin 履歴の
  決算短信開示日（期末後 80 日以内の最初の短信）を優先し、無ければ期末+45日で近似。
  バリュエーションは date 時点株価 × 直近公表 EPS / 推定 BPS で trailing PER/PBR を
  近似（note に「近似値」と明記）。次回決算日は yanoshin 履歴の date 直後の
  決算短信日付から逆引き。開示 PDF 要約は過去分が入手不可のため含めない。
- macro: data/macro_history.csv の date 以前 5 営業日窓（date 当日は含めない）。
  金利（JGB/FRED）の過去時点値は未取得のため rates は空 + note。
  市場レジーム（TOPIX 終値 vs 25日線・5日騰落率の機械判定）を
  market_regime に含める（v2、context_builder.market_regime_from_series を共用）。

価格キャッシュの作成:
    ../../../.venv/bin/python historical_context.py --fetch --start 2026-01-20 --end 2026-07-04

単体確認:
    ../../../.venv/bin/python historical_context.py 7203 2026-04-15
"""
import argparse
import json
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
REPO_ROOT = POC3_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data"

PRICES_CACHE = DATA_DIR / "prices_history.csv"
MACRO_CACHE = DATA_DIR / "macro_history.csv"
NEWS_HISTORY_DIR = DATA_DIR / "news_history"

sys.path.insert(0, str(POC3_DIR))
sys.path.insert(0, str(POC3_DIR.parent / "poc1_data_sources"))

from context_builder import (  # noqa: E402
    MAX_DISCLOSURE_ITEMS,
    MAX_NEWS_ITEMS,
    ContextBuildError,
    _load_json,
    _oku,
    _round,
    _stock_meta,
    _fundamentals_period,
    US_INDEX_TICKERS,
    US_SECTOR_TICKERS,
    boj_snapshot,
    context_to_json,
    japan_cpi_snapshot,
    japan_pmi_snapshot,
    jfc_sme_snapshot,
    market_regime_from_series,
    technical_from_window,
    us_market_snapshot,
    us_overnight_for_stock,
)
from universe import UNIVERSE, yf_tickers  # noqa: E402

JST = timezone(timedelta(hours=9))

TECHNICAL_WINDOW_BDAYS = 30   # テクニカル計算に使う営業日数（既存と同じ約30日窓）
MACRO_WINDOW_BDAYS = 5        # マクロスナップショットの営業日数（既存と同じ直近5日）
DISCLOSURE_LOOKBACK_DAYS = 90

# 決算短信の公表日近似: 期末 + TANSHIN_APPROX_DAYS 日
TANSHIN_APPROX_DAYS = 45
# yanoshin 履歴から短信を期に紐づける際の探索窓（期末後この日数以内の最初の短信）
TANSHIN_MATCH_WINDOW_DAYS = 80

MACRO_TICKER_LABELS = {
    "^N225": "日経平均",
    "1306.T": "TOPIX連動ETF",
    "JPY=X": "ドル円",
    "^GSPC": "S&P500",
    "^VIX": "VIX",
}
# 前夜のNY市場（v2.1）: 米国指数・セクターETF + universe 銘柄の ADR
MACRO_TICKER_LABELS.update(US_INDEX_TICKERS)
MACRO_TICKER_LABELS.update({t: f"米セクターETF {n}" for t, n in US_SECTOR_TICKERS.items()})
MACRO_TICKER_LABELS.update(
    {s["adr"]: f"{s['name']} ADR" for s in UNIVERSE if s.get("adr")})


def _to_date(d) -> date_cls:
    if isinstance(d, date_cls) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return datetime.strptime(str(d), "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# 価格キャッシュ（yfinance 一括取得）
# ---------------------------------------------------------------------------

def fetch_price_history(start: str, end: str) -> None:
    """universe 銘柄 + マクロ系列の日足を一括取得して CSV キャッシュする。"""
    import yfinance as yf

    def _download(tickers, out_path, label_map=None):
        raw = yf.download(
            tickers=tickers, start=start, end=end, interval="1d",
            auto_adjust=False, group_by="ticker", threads=False, progress=False,
        )
        frames = []
        for t in tickers:
            try:
                sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                print(f"  [warn] {t}: データなし", file=sys.stderr)
                continue
            sub = sub.dropna(how="all").reset_index()
            if sub.empty:
                print(f"  [warn] {t}: データなし", file=sys.stderr)
                continue
            sub["ticker"] = t
            if label_map:
                sub["label"] = label_map[t]
            frames.append(sub)
        if not frames:
            raise RuntimeError(f"取得失敗: {tickers}")
        df = pd.concat(frames, ignore_index=True)
        df.to_csv(out_path, index=False)
        print(f"saved: {out_path} ({df['ticker'].nunique()} tickers, {len(df)} rows, "
              f"{df['Date'].min()} -> {df['Date'].max()})")

    _download(yf_tickers(), PRICES_CACHE)
    _download(list(MACRO_TICKER_LABELS), MACRO_CACHE, MACRO_TICKER_LABELS)


def _drop_price_outliers(df: pd.DataFrame, threshold: float = 0.4) -> pd.DataFrame:
    """yfinance の異常行（例: 1306.T で一部日付だけ約1/10 の値）を除去する。

    ticker ごとに Close の中心化 rolling median（11本）からの乖離が
    threshold（40%）を超える行をデータ不良として落とす。
    """
    frames = []
    for ticker, g in df.sort_values("Date").groupby("ticker"):
        med = g["Close"].rolling(11, center=True, min_periods=3).median()
        bad = (g["Close"] / med - 1).abs() > threshold
        if bad.any():
            days = ", ".join(d.strftime("%Y-%m-%d") for d in g.loc[bad, "Date"])
            print(f"  [warn] {ticker}: 異常値 {int(bad.sum())} 行を除外 ({days})",
                  file=sys.stderr)
        frames.append(g[~bad])
    return pd.concat(frames, ignore_index=True)


@lru_cache(maxsize=1)
def _load_price_history() -> pd.DataFrame:
    if not PRICES_CACHE.exists():
        raise ContextBuildError(
            f"{PRICES_CACHE} がありません。"
            "historical_context.py --fetch --start ... --end ... を先に実行してください。"
        )
    df = pd.read_csv(PRICES_CACHE, parse_dates=["Date"])
    return _drop_price_outliers(df).sort_values("Date")


@lru_cache(maxsize=1)
def _load_macro_history() -> pd.DataFrame:
    if not MACRO_CACHE.exists():
        raise ContextBuildError(
            f"{MACRO_CACHE} がありません。"
            "historical_context.py --fetch --start ... --end ... を先に実行してください。"
        )
    df = pd.read_csv(MACRO_CACHE, parse_dates=["Date"])
    return _drop_price_outliers(df).sort_values("Date")


def trading_dates(code: str, start=None, end=None) -> list:
    """価格キャッシュ上の当該銘柄の営業日（date のリスト、昇順）。"""
    df = _load_price_history()
    px = df[df["ticker"] == f"{code}.T"]
    dates = sorted({d.date() for d in px["Date"]})
    if start:
        start = _to_date(start)
        dates = [d for d in dates if d >= start]
    if end:
        end = _to_date(end)
        dates = [d for d in dates if d <= end]
    return dates


# ---------------------------------------------------------------------------
# 各セクション（as-of 版）
# ---------------------------------------------------------------------------

def build_technical_asof(code: str, asof) -> dict:
    """date の朝時点のテクニカル（最終足は date の前営業日）。"""
    asof = _to_date(asof)
    df = _load_price_history()
    ticker = f"{code}.T"
    px = df[(df["ticker"] == ticker) & (df["Date"].dt.date < asof)].copy()
    px = px.tail(TECHNICAL_WINDOW_BDAYS)
    if len(px) < 26:
        raise ContextBuildError(
            f"{ticker} の {asof} 以前の価格データが不足しています（{len(px)} 行）。"
        )
    return technical_from_window(px)


def _published_date(item: dict):
    """RSS の published（RFC822）を date に変換する。パース不能は None。"""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(item.get("published") or "").date()
    except (TypeError, ValueError):
        return None


def build_news_asof(code: str, asof) -> dict:
    """fetch_news_range.py のキャッシュから date 直前3日分・最大10件。

    Google News の before: 指定は当日分が混入することがあるため、
    published < asof のもののみ残す（look-ahead 防止）。
    """
    asof = _to_date(asof)
    path = NEWS_HISTORY_DIR / code / f"{asof.isoformat()}.json"
    if not path.exists():
        raise ContextBuildError(
            f"{path} がありません。fetch_news_range.py を先に実行してください。"
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    filtered = [
        it for it in data.get("items", [])
        if (_published_date(it) or asof) < asof
    ]
    items = filtered[:MAX_NEWS_ITEMS]
    return {
        "source": f"Google News RSS（{asof} 直前3日分の日付指定検索）",
        "fetched_at": data.get("fetched_at"),
        "total_count": len(filtered),
        "headlines": [
            {
                "title": it.get("title"),
                "published": it.get("published"),
                "publisher": it.get("source"),
            }
            for it in items
        ],
    }


def _disclosure_items(code: str) -> list:
    """yanoshin 履歴の当該銘柄開示（pubdate 降順、日付は date 型に変換済み）。"""
    data = _load_json("disclosures_yanoshin.json")
    entry = (data.get("disclosures") or {}).get(code) or {}
    items = []
    for it in entry.get("items", []):
        pubdate = it.get("pubdate") or ""
        try:
            d = datetime.strptime(pubdate[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        items.append({"date": d, "pubdate": pubdate, "title": it.get("title") or ""})
    items.sort(key=lambda x: x["pubdate"], reverse=True)
    return items


def build_disclosures_asof(code: str, asof) -> dict:
    """yanoshin 履歴から date 以前 90 日・最大 10 件のタイトル。"""
    asof = _to_date(asof)
    cutoff = asof - timedelta(days=DISCLOSURE_LOOKBACK_DAYS)
    items = [
        it for it in _disclosure_items(code)
        if cutoff <= it["date"] < asof
    ][:MAX_DISCLOSURE_ITEMS]
    return {
        "source": "TDnet 適時開示（yanoshin API、as-of 以前90日）",
        "fetched_at": None,
        "items": [{"date": it["pubdate"], "title": it["title"]} for it in items],
    }


def _tanshin_dates(code: str) -> list:
    """yanoshin 履歴のうち決算短信の開示日（昇順）。"""
    return sorted(
        it["date"] for it in _disclosure_items(code) if "決算短信" in it["title"]
    )


def _period_end(period: str) -> date_cls:
    """'2026-03' → 2026-03-31（月末）。"""
    ts = pd.Period(period, freq="M").end_time
    return ts.date()


def _estimated_pub_date(period_end: date_cls, tanshin: list) -> date_cls:
    """期末に対応する公表日の推定。

    yanoshin 履歴に「期末後 80 日以内の最初の決算短信」があればその日付、
    無ければ期末 + 45 日で近似する。
    """
    for d in tanshin:
        if period_end < d <= period_end + timedelta(days=TANSHIN_MATCH_WINDOW_DAYS):
            return d
    return period_end + timedelta(days=TANSHIN_APPROX_DAYS)


def _last_close_asof(code: str, asof: date_cls):
    """asof 前営業日の終値（= シグナル生成時に見えている直近株価）。"""
    df = _load_price_history()
    px = df[(df["ticker"] == f"{code}.T") & (df["Date"].dt.date < asof)]
    if px.empty:
        return None
    return float(px["Close"].iloc[-1])


def _latest_close(code: str):
    """キャッシュ上の最新終値（現在のバリュエーションから BPS 等を逆算する用）。"""
    df = _load_price_history()
    px = df[df["ticker"] == f"{code}.T"]
    if px.empty:
        return None
    return float(px["Close"].iloc[-1])


def build_fundamentals_asof(code: str, asof) -> dict:
    """date 時点で公表済みと推定される業績のみ + 近似バリュエーション。"""
    asof = _to_date(asof)
    out = {
        "source": "yfinance（業績・EPS）+ TDnet 決算短信日付（yanoshin）から as-of 時点を再構成",
    }
    notes = [
        "ヒストリカル再構成のため、バリュエーションは as-of 時点株価 × 直近公表 EPS/推定 BPS による近似値",
        "重要開示の本文要約は過去分は入手不可（タイトルは disclosures 参照）",
    ]

    entry = None
    try:
        data = _load_json("fundamentals_yfinance.json")
        entry = (data.get("fundamentals") or {}).get(code)
    except ContextBuildError:
        notes.append("fundamentals_yfinance.json が未取得")

    if entry is None and "fundamentals_yfinance.json が未取得" not in notes:
        notes.append("この銘柄の yfinance ファンダメンタルズが未取得")

    if entry:
        tanshin = _tanshin_dates(code)

        def published(periods):
            keep = []
            for p in periods or []:
                if not p.get("period"):
                    continue
                pub = _estimated_pub_date(_period_end(p["period"]), tanshin)
                if pub < asof:
                    keep.append(p)
            return keep

        annual_pub = published(entry.get("annual"))
        quarterly_pub = published(entry.get("quarterly"))

        # --- 近似バリュエーション ---
        price_asof = _last_close_asof(code, asof)
        price_now = _latest_close(code)
        val_now = entry.get("valuation") or {}
        valuation = {}
        if price_asof is not None:
            latest_eps = next(
                (p["diluted_eps"] for p in annual_pub if p.get("diluted_eps")), None
            )
            if latest_eps:
                valuation["trailing_pe"] = round(price_asof / latest_eps, 3)
            if price_now and val_now.get("price_to_book"):
                bps = price_now / val_now["price_to_book"]
                valuation["price_to_book"] = round(price_asof / bps, 3)
            if price_now and val_now.get("dividend_yield_percent"):
                div = val_now["dividend_yield_percent"] * price_now / 100.0
                valuation["dividend_yield_percent"] = round(div / price_asof * 100, 2)
            if price_now and val_now.get("market_cap_jpy"):
                shares = val_now["market_cap_jpy"] / price_now
                valuation["market_cap_oku_jpy"] = _oku(shares * price_asof)
        if valuation:
            valuation["note"] = "as-of 時点株価と直近公表 EPS/現在の BPS・配当・株式数から逆算した近似値"
            out["valuation"] = valuation
        else:
            notes.append("as-of 時点のバリュエーションを近似できるデータなし")

        # --- 業績トレンド（公表済みのみ） ---
        annual = [_fundamentals_period(p) for p in annual_pub]
        quarterly = [_fundamentals_period(p) for p in quarterly_pub]
        if annual or quarterly:
            out["earnings_trend"] = {
                "unit_note": "金額は億円（*_oku_jpy）。EPS は円。as-of 時点で公表済みと推定される期のみ。",
            }
            if annual:
                out["earnings_trend"]["annual"] = annual
            else:
                notes.append("as-of 時点で公表済みの年次業績なし")
            if quarterly:
                out["earnings_trend"]["quarterly"] = quarterly
            else:
                notes.append("as-of 時点で公表済みの四半期業績なし")
        else:
            notes.append("as-of 時点で公表済みの業績トレンドデータなし")

        # --- 次回決算日（yanoshin 履歴の date 直後の決算短信日付から逆引き） ---
        next_tanshin = next((d for d in tanshin if d >= asof), None)
        if next_tanshin:
            out["next_earnings_date"] = next_tanshin.isoformat()
        else:
            notes.append("次回決算発表日は不明（as-of 以降の決算短信が履歴にない）")

    out["note"] = "欠損・近似: " + " / ".join(notes)
    return out


def build_macro_asof(asof) -> dict:
    """date の朝時点のマクロスナップショット（最終足は前営業日）。"""
    asof = _to_date(asof)
    df = _load_macro_history()

    indices = []
    for ticker, label in MACRO_TICKER_LABELS.items():
        series = df[(df["ticker"] == ticker) & (df["Date"].dt.date < asof)]
        series = series.dropna(subset=["Close"]).tail(MACRO_WINDOW_BDAYS)
        if series.empty:
            continue
        last = series.iloc[-1]
        first = series.iloc[0]
        prev_close = series["Close"].iloc[-2] if len(series) >= 2 else None
        change_1d = (last["Close"] / prev_close - 1) * 100 if prev_close else None
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

    # 市場レジーム（TOPIX 25日線には asof 以前の全履歴を使う）
    topix = df[(df["ticker"] == "1306.T") & (df["Date"].dt.date < asof)]
    topix = topix.dropna(subset=["Close"])
    regime = market_regime_from_series(topix.set_index("Date")["Close"])

    macro = {
        "indices": indices,
        "market_regime": regime,
        "rates": {},
        "note": "ヒストリカル再構成のため金利（日米10年金利等）の過去時点値は未取得",
    }
    # 日本公庫 中小企業景況調査 DI（調査月の翌月1日以降にのみ利用可 = look-ahead 防止）
    sme = jfc_sme_snapshot(asof=asof)
    if sme:
        macro["jfc_sme_survey"] = sme
    # 日銀（決定会合日程 + 短観。新着発表は遡及不可のため asof 指定時は含まれない）
    boj = boj_snapshot(asof=asof)
    if boj:
        macro["boj"] = boj
    # 前夜のNY市場（Date < asof の最終NY営業日 = asof の朝に確定済みの終値）
    us = us_market_snapshot(df, asof=asof)
    if us:
        macro["us_market_overnight"] = us
    # 日本PMI（公表タイミングによる look-ahead ガード付き）
    pmi = japan_pmi_snapshot(asof=asof)
    if pmi:
        macro["japan_pmi"] = pmi
    # 全国CPI（実公表日の翌日以降のみ = look-ahead 防止）
    cpi = japan_cpi_snapshot(asof=asof)
    if cpi:
        macro["japan_cpi"] = cpi
    return macro


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def build_context_asof(code: str, asof) -> dict:
    """1銘柄 × 過去営業日 as-of の分析入力コンテキスト（既存 build_context と同一構造）。"""
    asof = _to_date(asof)
    meta = _stock_meta(code)
    context = {
        "meta": {
            "code": code,
            "name": meta["name"],
            "ticker": f"{code}.T",
        },
        "price_technical": build_technical_asof(code, asof),
        "fundamentals": build_fundamentals_asof(code, asof),
        "news": build_news_asof(code, asof),
        "disclosures": build_disclosures_asof(code, asof),
        "macro": build_macro_asof(asof),
    }
    us = us_overnight_for_stock(code, _load_macro_history(), asof=asof)
    if us:
        context["us_overnight"] = us
    return context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", nargs="?", help="銘柄コード")
    parser.add_argument("asof", nargs="?", help="as-of 日 YYYY-MM-DD")
    parser.add_argument("--fetch", action="store_true", help="価格キャッシュを取得")
    parser.add_argument("--start", help="--fetch の開始日")
    parser.add_argument("--end", help="--fetch の終了日")
    args = parser.parse_args()

    if args.fetch:
        if not (args.start and args.end):
            parser.error("--fetch には --start と --end が必要です")
        fetch_price_history(args.start, args.end)
        return

    if not (args.code and args.asof):
        parser.error("code と asof を指定してください（例: 7203 2026-04-15）")
    print(context_to_json(build_context_asof(args.code, args.asof)))


if __name__ == "__main__":
    main()
