"""全国消費者物価指数（CPI、総務省統計局）の月次速報を取得する。

ユーザー要望「stat.go.jp の CPI 速報も材料に」への対応。
https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html（Shift_JIS の静的 HTML）
から最新月の以下を抽出する:

- 総合指数（headline）
- 生鮮食品を除く総合指数（core、日銀が重視するコア CPI）
- 生鮮食品及びエネルギーを除く総合指数（core-core）
それぞれ 2020年=100 の指数水準と前年同月比（%）、および公表日。

ページには最新月しか載らないため、毎朝の実行で data/macro_cpi.json に
月次系列をマージして自前の時系列を蓄積する（PMI と同方式）。
FRED の日本 CPI（OECD 系列）は 2021 年で提供終了しておりバックフィル不可。
過去系列の一括取得が必要になったら e-Stat API（要 appId 登録）を検討する。

出力: data/macro_cpi.json
"""
import json
import re
import sys

import requests

from common import DATA_DIR, Timer, now_jst_iso, print_summary, save_json

SOURCE = "macro_cpi"

CPI_URL = "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"

HEADER_PATTERN = re.compile(
    r"(\d{4})年（令和\d+年）(\d{1,2})月分（(\d{4})年(\d{1,2})月(\d{1,2})日公表）")
# 例: 「総合指数は2020年を100として113.5」「前年同月比は1.5%の上昇」
INDEX_PATTERNS = {
    "headline": re.compile(
        r"総合指数は2020年を100として([\d.]+)[\s\S]{0,80}?"
        r"前年同月比は([\d.]+)[%％]?の(上昇|下落)"),
    "core": re.compile(
        r"生鮮食品を除く総合指数は([\d.]+)[\s\S]{0,80}?"
        r"前年同月比は([\d.]+)[%％]?の(上昇|下落)"),
    "core_core": re.compile(
        r"生鮮食品及びエネルギーを除く総合指数は([\d.]+)[\s\S]{0,80}?"
        r"前年同月比は([\d.]+)[%％]?の(上昇|下落)"),
}


def parse_page(text: str) -> "tuple[str, dict]":
    """速報ページ本文から (対象月 YYYY-MM, レコード) を抽出する。"""
    m = HEADER_PATTERN.search(text)
    if not m:
        raise RuntimeError("CPI 速報の対象月・公表日が見つかりません（ページ構成変更?）")
    dy, dm, py, pm, pd_ = map(int, m.groups())
    month_key = f"{dy:04d}-{dm:02d}"
    record = {"published": f"{py:04d}-{pm:02d}-{pd_:02d}"}
    for kind, pat in INDEX_PATTERNS.items():
        im = pat.search(text)
        if not im:
            raise RuntimeError(f"CPI {kind} の値が見つかりません（ページ構成変更?）")
        level, yoy, direction = im.groups()
        sign = 1 if direction == "上昇" else -1
        record[f"{kind}_index"] = float(level)
        record[f"{kind}_yoy_pct"] = round(sign * float(yoy), 2)
    return month_key, record


def main() -> dict:
    with Timer() as t:
        try:
            resp = requests.get(CPI_URL, timeout=30)
            resp.raise_for_status()
            text = re.sub(r"<[^>]+>", " ", resp.content.decode("cp932"))
            text = re.sub(r"[\s　]+", "", text)  # 全角空白・改行を除去して連結
            month_key, record = parse_page(text)

            path = DATA_DIR / "macro_cpi.json"
            series = {}
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    series = (json.load(f).get("series") or {})
            series[month_key] = record
            out = {
                "source": "総務省統計局 消費者物価指数（CPI）全国・月次速報",
                "source_url": CPI_URL,
                "fetched_at": now_jst_iso(),
                "columns_note": {
                    "headline": "総合指数（2020年=100）",
                    "core": "生鮮食品を除く総合（コアCPI、日銀が重視）",
                    "core_core": "生鮮食品及びエネルギーを除く総合（コアコアCPI）",
                },
                "publication_note": ("公表はおおむね対象月の翌月19日前後（8:30）。"
                                     "published は実際の公表日。利用側は公表日の"
                                     "翌日以降に利用可とみなす"),
                "series": dict(sorted(series.items())),
            }
            save_json("macro_cpi.json", out)
            note = (f"{month_key}: 総合 {record['headline_yoy_pct']:+.1f}% / "
                    f"コア {record['core_yoy_pct']:+.1f}% / "
                    f"コアコア {record['core_core_yoy_pct']:+.1f}%（蓄積 {len(series)} ヶ月）")
            ok, count = True, len(series)
        except Exception as e:
            ok, count, note = False, 0, f"{type(e).__name__}: {e}"
    print_summary(SOURCE, ok, count, t.seconds, note)
    return {"source": SOURCE, "ok": ok, "count": count,
            "seconds": t.seconds, "note": note, "fetched_at": now_jst_iso()}


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
