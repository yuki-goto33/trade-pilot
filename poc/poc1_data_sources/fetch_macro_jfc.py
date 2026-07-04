"""日本政策金融公庫「中小企業景況調査」（月次）の主要 DI 時系列を取得する。

出典: https://www.jfc.go.jp/n/findings/tyousa_sihanki.html
時系列 CSV: https://www.jfc.go.jp/n/findings/csv/kdi_<YYMM>.csv（UTF-8 BOM 付き）

取得する DI（ユーザー要望: 販売価格 DI をファンダ/マクロ入力に追加）:
- 売上げDI（季節調整値）・売上げ見通しDI（季節調整値）・利益額DI（季節調整値）
- 販売価格DI・仕入価格DI（原数値）

公表タイミング: 各月調査はおおむね当該月の月末に公表される
（例: 2026年6月調査 → 2026-06-30 公表）。look-ahead 防止のため、
利用側では「調査月の翌月1日以降に利用可能」とみなす。

注意: 季節調整値は毎年1月調査の公表時に遡及改訂される（CSV 冒頭の注記）。
ヒストリカル利用時は「当時公表された値と厳密には一致しない可能性」がある。

出力: data/macro_jfc.json（全期間の月次系列）
"""
import csv
import io
import re
import sys
from datetime import datetime

import requests

from common import JST, Timer, now_jst_iso, print_summary, save_json

SOURCE = "macro_jfc"

CSV_URL_TEMPLATE = "https://www.jfc.go.jp/n/findings/csv/kdi_{yymm}.csv"
LOOKBACK_MONTHS = 4  # 最新月の CSV が未公開の場合に遡る月数

# CSV の列インデックス（ヘッダー3行目基準。実データ行で確認済み）
COLUMNS = {
    2: "sales_di",            # 売上げDI（季節調整値）
    3: "sales_forecast_di",   # 売上げ見通しDI（季節調整値）
    4: "profit_di",           # 利益額DI（季節調整値）
    11: "sales_price_di",     # 販売価格DI
    12: "purchase_price_di",  # 仕入価格DI
}


def candidate_urls() -> "list[str]":
    """今月から過去 LOOKBACK_MONTHS ヶ月分の CSV URL 候補（新しい順）。"""
    now = datetime.now(JST)
    year, month = now.year, now.month
    urls = []
    for _ in range(LOOKBACK_MONTHS + 1):
        urls.append(CSV_URL_TEMPLATE.format(yymm=f"{year % 100:02d}{month:02d}"))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return urls


def fetch_latest_csv() -> "tuple[str, str]":
    """最新の時系列 CSV を取得して (テキスト, URL) を返す。"""
    last_err = None
    for url in candidate_urls():
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content.decode("utf-8-sig"), url
        last_err = f"HTTP {resp.status_code}: {url}"
    raise RuntimeError(f"JFC 時系列 CSV が見つかりません（{last_err}）")


def parse_series(text: str) -> "list[dict]":
    """年・月の行構造（年は年始行のみ記載）から月次レコードを組み立てる。"""
    records = []
    year = None
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        ym = re.match(r"^(\d{4})年", (row[0] or "").strip())
        if ym:
            year = int(ym.group(1))
        mm = re.match(r"^(\d{1,2})月$", (row[1] or "").strip()) if len(row) > 1 else None
        if year is None or not mm:
            continue
        month = int(mm.group(1))
        rec = {"year_month": f"{year:04d}-{month:02d}"}
        has_value = False
        for idx, key in COLUMNS.items():
            raw = row[idx].strip() if len(row) > idx else ""
            try:
                rec[key] = float(raw)
                has_value = True
            except ValueError:
                rec[key] = None
        if has_value:
            records.append(rec)
    records.sort(key=lambda r: r["year_month"])
    return records


def main() -> dict:
    with Timer() as t:
        try:
            text, url = fetch_latest_csv()
            series = parse_series(text)
            if not series:
                raise RuntimeError("CSV のパース結果が空です（列構造が変わった可能性）")
            out = {
                "source": "日本政策金融公庫 中小企業景況調査（月次）主要DI時系列",
                "source_url": url,
                "fetched_at": now_jst_iso(),
                "publication_note": (
                    "各月調査はおおむね当該月の月末に公表。look-ahead 防止のため"
                    "「調査月の翌月1日以降に利用可能」とみなすこと。"
                    "季節調整値は毎年1月調査の公表時に遡及改訂される。"
                ),
                "columns_note": {
                    "sales_di": "売上げDI（季節調整値）",
                    "sales_forecast_di": "売上げ見通しDI（季節調整値）",
                    "profit_di": "利益額DI（季節調整値）",
                    "sales_price_di": "販売価格DI",
                    "purchase_price_di": "仕入価格DI",
                },
                "series": series,
            }
            path = save_json("macro_jfc.json", out)
            note = f"{series[0]['year_month']}〜{series[-1]['year_month']} -> {path.name}"
            ok, count = True, len(series)
        except Exception as e:
            ok, count, note = False, 0, f"{type(e).__name__}: {e}"
    print_summary(SOURCE, ok, count, t.seconds, note)
    return {"source": SOURCE, "ok": ok, "count": count,
            "seconds": t.seconds, "note": note, "fetched_at": now_jst_iso()}


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
