"""yfinance でユニバース銘柄のファンダメンタルズ（バリュエーション・業績）を取得する。

シグナル生成のコンテキストに「バリュエーション・業績トレンド・次回決算日」を
供給するためのスクリプト。取得元は yfinance の Ticker.info / financials /
quarterly_financials / calendar（earnings_dates は lxml 依存のためフォールバック扱い）。

- バリュエーション: trailingPE / forwardPE / priceToBook / dividendYield /
  marketCap / returnOnEquity（info は銘柄により欠損が多い → フィールド単位で null）
- 業績トレンド: 年次の売上高・営業利益・純利益（直近3期）+ 四半期の直近4Q
  （行名は Total Revenue / Operating Income / Net Income。無い行は null）
- 次回決算発表日: calendar の Earnings Date（無ければ earnings_dates、それも
  無ければ null）
- 1req/秒スロットリング。銘柄単位の取得失敗はスキップして続行
- 出力: data/fundamentals_yfinance.json
"""
import sys
import time
import warnings
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from common import Timer, now_jst_iso, parse_universe_arg, print_summary, save_json
from universe import load_universe

warnings.filterwarnings("ignore", category=FutureWarning)

SOURCE = "fundamentals_yfinance"
OUT_NAME = "fundamentals_yfinance.json"
REQUEST_INTERVAL_SEC = 1.0

# info から取得するバリュエーション系フィールド（info キー → 出力キー）
INFO_FIELDS = {
    "trailingPE": "trailing_pe",
    "forwardPE": "forward_pe",
    "priceToBook": "price_to_book",
    "dividendYield": "dividend_yield_percent",  # yfinance>=0.2.5x は % 値
    "marketCap": "market_cap_jpy",
    "returnOnEquity": "return_on_equity",
}

# financials / quarterly_financials から取得する行（出力キー → 行名の候補列）。
# 行名は業種により異なる（例: 銀行は Operating Income を持たない）ため、
# 先頭から順に存在する行を採用する防御的な作り。
FINANCIAL_ROWS = {
    "revenue_jpy": ["Total Revenue", "Operating Revenue"],
    "operating_income_jpy": ["Operating Income", "Total Operating Income As Reported"],
    "net_income_jpy": ["Net Income"],
    # 日本株の quarterly_financials は売上・利益が NaN のことが多く、
    # EPS のみ収載されているケースが大半のため補助指標として持つ
    "diluted_eps": ["Diluted EPS"],
}

ANNUAL_PERIODS = 3
QUARTERLY_PERIODS = 4


def _clean_number(v):
    """NaN/None を null に、それ以外を float に正規化する。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return f


def _extract_valuation(info: dict) -> dict:
    """info dict からバリュエーション系フィールドを抜き出す（欠損は null）。"""
    return {out: _clean_number(info.get(key)) for key, out in INFO_FIELDS.items()}


def _extract_statement(df, n_periods: int) -> list:
    """financials 系 DataFrame から直近 n 期分の売上・営業利益・純利益を抜き出す。

    yfinance の列は新しい期が先頭。列名（Timestamp）を 'YYYY-MM' 形式の
    period ラベルにする。行が無い/NaN のフィールドは null。
    全フィールドが null の期（データ未収載）は除外する。
    """
    if df is None or df.empty:
        return []
    periods = []
    for col in list(df.columns):
        if len(periods) >= n_periods:
            break
        label = col.strftime("%Y-%m") if hasattr(col, "strftime") else str(col)
        entry = {"period": label}
        for out, row_candidates in FINANCIAL_ROWS.items():
            f = None
            for row in row_candidates:
                if row in df.index:
                    f = _clean_number(df.at[row, col])
                    if f is not None:
                        break
            if f is None:
                entry[out] = None
            else:
                # 円建て金額は整数、EPS 等は小数2桁
                entry[out] = int(f) if out.endswith("_jpy") else round(f, 2)
        if any(v is not None for k, v in entry.items() if k != "period"):
            periods.append(entry)
    return periods


def _to_iso_date(v):
    """calendar / earnings_dates の日付表現を ISO 文字列にする。"""
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    return None


def _next_earnings_date(ticker: yf.Ticker):
    """次回決算発表日を calendar → earnings_dates の順で探す（無ければ None）。

    calendar には過去の発表日が残っていることがある（例: 発表済みの前回分）ため、
    「今日以降」の日付のみを採用する。
    """
    today_iso = date.today().strftime("%Y-%m-%d")

    # 1) calendar（HTML パーサ不要で軽い）
    try:
        cal = ticker.calendar
        dates = (cal or {}).get("Earnings Date") or []
        future = sorted(d for d in (_to_iso_date(x) for x in dates)
                        if d and d >= today_iso)
        if future:
            return future[0]
    except Exception as e:  # noqa: BLE001 - フィールド単位の失敗は握って null
        print(f"  calendar 取得失敗: {e}", file=sys.stderr)

    # 2) earnings_dates（lxml が無い環境では ImportError になるため防御）
    try:
        ed = ticker.earnings_dates
        if ed is not None and not ed.empty:
            future = sorted(d for d in (_to_iso_date(ts) for ts in ed.index)
                            if d and d >= today_iso)
            if future:
                return future[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_one(code: str) -> dict:
    """1銘柄分のファンダメンタルズを取得する（フィールド単位の欠損は null）。"""
    ticker = yf.Ticker(f"{code}.T")

    try:
        info = ticker.info or {}
    except Exception as e:  # noqa: BLE001
        print(f"  {code}: info 取得失敗 ({e})", file=sys.stderr)
        info = {}

    try:
        annual = _extract_statement(ticker.financials, ANNUAL_PERIODS)
    except Exception as e:  # noqa: BLE001
        print(f"  {code}: financials 取得失敗 ({e})", file=sys.stderr)
        annual = []

    try:
        quarterly = _extract_statement(ticker.quarterly_financials, QUARTERLY_PERIODS)
    except Exception as e:  # noqa: BLE001
        print(f"  {code}: quarterly_financials 取得失敗 ({e})", file=sys.stderr)
        quarterly = []

    return {
        "ticker": f"{code}.T",
        "valuation": _extract_valuation(info),
        "annual": annual,
        "quarterly": quarterly,
        "next_earnings_date": _next_earnings_date(ticker),
    }


def fetch(universe) -> "tuple[dict, list]":
    result = {}
    failed = []
    for i, stock in enumerate(universe):
        code = stock["code"]
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)
        try:
            result[code] = fetch_one(code)
        except Exception as e:  # noqa: BLE001 - 銘柄単位の失敗はスキップして続行
            print(f"  {code}: 取得失敗 ({type(e).__name__}: {e})", file=sys.stderr)
            failed.append(code)
    return result, failed


def main(universe_path=None) -> dict:
    universe = load_universe(universe_path)
    with Timer() as t:
        try:
            fundamentals, failed = fetch(universe)
            if not fundamentals:
                raise RuntimeError("全銘柄の取得に失敗")
            out = save_json(OUT_NAME, {
                "fetched_at": now_jst_iso(),
                "failed": failed,
                "fundamentals": fundamentals,
            })
            note = f"{len(fundamentals)}/{len(universe)} 銘柄, 保存先 {out.name}"
            if failed:
                note += f", 失敗: {failed}"
            summary = {"source": SOURCE, "ok": True, "count": len(fundamentals), "note": note}
        except Exception as e:  # noqa: BLE001
            summary = {"source": SOURCE, "ok": False, "count": 0,
                       "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main(parse_universe_arg(__doc__))["ok"] else 1)
