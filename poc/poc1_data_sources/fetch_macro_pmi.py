"""日本の PMI（製造業・サービス業・複合）を取得する。

ユーザー要望「S&P グローバルの日本サービス業PMI・日本製造業PMIを材料に」への対応。

原典は S&P Global / auじぶん銀行の「auじぶん銀行日本PMI」だが、
S&P のサイト（pmi.spglobal.com）は AWS WAF の JS チャレンジで保護されており
ヘッドレス取得不可のため、TradingEconomics の公開ページ（静的 HTML の
description に最新値・前月値が含まれる）から取得する。

- 静的 HTML からは「最新月・前月」の2点のみ取れるため、毎朝の実行で
  data/macro_pmi.json に月次系列をマージして自前の時系列を蓄積する
- 公表タイミング（look-ahead 防止は利用側 context_builder が担う）:
  製造業（改定値）= 翌月第1営業日 / サービス業・複合 = 翌月第3営業日
- 注意: TradingEconomics のスクレイピングは PoC（個人利用・1日1回）に限る。
  本番では S&P Global のライセンス取得または有料 API を要件とすること

出力: data/macro_pmi.json
"""
import json
import re
import sys

import requests

from common import DATA_DIR, Timer, now_jst_iso, print_summary, save_json

SOURCE = "macro_pmi"

TE_URL_TEMPLATE = "https://tradingeconomics.com/japan/{slug}"
SERIES = {
    "manufacturing": {"slug": "manufacturing-pmi", "label": "日本製造業PMI"},
    "services": {"slug": "services-pmi", "label": "日本サービス業PMI"},
    "composite": {"slug": "composite-pmi", "label": "日本複合PMI"},
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.9",
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}

# 静的ページから遡れない既知の値（TE 本文の言及から確認、2026-07-05 時点）
SEED = {
    "manufacturing": {"2026-04": 55.1},
}

DESC_PATTERN = re.compile(
    r"(?:Manufacturing|Services|Composite) PMI in Japan "
    r"(?:increased|decreased|rose|fell|climbed|dropped|edged (?:up|down)|"
    r"went (?:up|down)|was revised [a-z]+)? ?to ([\d.]+) points? in (\w+) "
    r"from ([\d.]+) points? in (\w+) of (\d{4})")
UNCHANGED_PATTERN = re.compile(
    r"(?:Manufacturing|Services|Composite) PMI in Japan (?:was unchanged|"
    r"remained (?:unchanged|steady)) at ([\d.]+) points? in (\w+) of (\d{4})")


def _month_key(year: int, month_name: str, latest_month: int = None) -> str:
    """英語月名 → YYYY-MM。前月が年をまたぐ場合（1月の前=12月）は年を補正する。"""
    m = MONTHS[month_name]
    if latest_month is not None and m > latest_month:
        year -= 1
    return f"{year:04d}-{m:02d}"


def parse_page(html: str) -> "tuple[dict, str]":
    """TE ページから {YYYY-MM: value} と英文サマリーを抽出する。"""
    values = {}
    m = DESC_PATTERN.search(html)
    if m:
        cur_v, cur_mon, prev_v, prev_mon, year = m.groups()
        year = int(year)
        cur_key = _month_key(year, cur_mon)
        values[cur_key] = float(cur_v)
        values[_month_key(year, prev_mon, latest_month=MONTHS[cur_mon])] = float(prev_v)
    else:
        m = UNCHANGED_PATTERN.search(html)
        if m:
            v, mon, year = m.groups()
            values[_month_key(int(year), mon)] = float(v)
    if not values:
        raise RuntimeError("PMI 値のパースに失敗（ページ構成変更の可能性）")

    # 本文の解説パラグラフ（最新月の詳細。歴史的水準・要因などの文脈）。
    # meta description（"This page provides..." を含む定型文）は除外し、
    # 候補のうち最長のものを採用する
    summary = None
    candidates = [
        c for c in re.findall(
            r"(?:Manufacturing|Services|Composite) PMI (?:in Japan )?(?:was|"
            r"remained|rose|fell|increased|decreased|came|stood)[^<>]{150,}", html)
        if "This page" not in c[:200]
    ]
    if candidates:
        summary = max(candidates, key=len)[:600].rsplit(". ", 1)[0] + "."
    return values, summary


def main() -> dict:
    with Timer() as t:
        try:
            # 既存系列を読み込んでマージ（毎朝の実行で時系列を蓄積する）
            path = DATA_DIR / "macro_pmi.json"
            existing = {}
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    existing = {k: dict(v) for k, v in
                                (json.load(f).get("series") or {}).items()}

            series, summaries, fetched = {}, {}, 0
            for kind, cfg in SERIES.items():
                merged = dict(SEED.get(kind, {}))
                merged.update(existing.get(kind, {}))
                url = TE_URL_TEMPLATE.format(slug=cfg["slug"])
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=30)
                    resp.raise_for_status()
                    values, summary = parse_page(resp.text)
                    merged.update(values)
                    if summary:
                        summaries[kind] = summary
                    fetched += 1
                except Exception as e:
                    print(f"  [warn] {kind}: {type(e).__name__}: {e}",
                          file=sys.stderr)
                series[kind] = dict(sorted(merged.items()))
            if fetched == 0:
                raise RuntimeError("全系列の取得に失敗")

            out = {
                "source": "S&P Global / auじぶん銀行 日本PMI（TradingEconomics 経由）",
                "source_note": ("原典 pmi.spglobal.com は WAF 保護のため TE の公開"
                                "ページから取得。PoC 限定・本番はライセンス要"),
                "fetched_at": now_jst_iso(),
                "publication_note": ("改定値の公表: 製造業=翌月第1営業日 / "
                                     "サービス業・複合=翌月第3営業日。"
                                     "利用側は翌月2日/6日以降に利用可とみなす"),
                "series": series,
                "summaries_en": summaries,
            }
            save_json("macro_pmi.json", out)
            counts = {k: len(v) for k, v in series.items()}
            note = f"{counts} -> macro_pmi.json"
            ok, count = True, sum(counts.values())
        except Exception as e:
            ok, count, note = False, 0, f"{type(e).__name__}: {e}"
    print_summary(SOURCE, ok, count, t.seconds, note)
    return {"source": SOURCE, "ok": ok, "count": count,
            "seconds": t.seconds, "note": note, "fetched_at": now_jst_iso()}


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
