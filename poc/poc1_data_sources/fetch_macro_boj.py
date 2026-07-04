"""日本銀行の株式市場影響の大きい情報を取得する。

ユーザー要望「日銀HPの情報から日経の株に影響が大きいものを判断材料に」を受けて、
以下の3系統を取得する:

1. 金融政策決定会合の開催日程（年間スケジュール、事前公表）
   - https://www.boj.or.jp/mopo/mpmsche_minu/index.htm をスクレイプ
   - 失敗時は 2026 年の確認済み日程にフォールバック
   - 用途: 「次回会合までの日数」= 金利イベントリスク（決算またぎと同様）
2. 短観（全国企業短期経済観測調査）の業況判断 DI
   - 概要 ZIP（Excel 計表1）から 大企業/中小企業 × 製造業/非製造業/全産業 の
     「最近・前回比変化幅・先行き」を抽出。直近2回分の ZIP で4調査分をカバー
   - 公表はおおむね 3/6/9 月調査 → 翌月1日、12月調査 → 12月中旬。
     look-ahead 防止のため「公表日の翌日から利用可能」とみなす
3. 新着発表（What's New RSS: https://www.boj.or.jp/rss/whatsnew.xml）
   - 金融政策・オペ・展望レポート等の重要キーワードでフィルタ
   - 過去分は遡及不可（フォワード運用でのみ利用。ニュースと同じ制約）

出力: data/macro_boj.json
"""
import io
import re
import sys
import zipfile
from datetime import date, datetime, timedelta

import feedparser
import pandas as pd
import requests

from common import JST, Timer, now_jst_iso, print_summary, save_json

SOURCE = "macro_boj"

MPM_SCHEDULE_URL = "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
RSS_URL = "https://www.boj.or.jp/rss/whatsnew.xml"
TANKAN_ZIP_TEMPLATE = "https://www.boj.or.jp/statistics/tk/gaiyo/{year}/tka{yy}{mm:02d}.zip"

# スクレイプ失敗時のフォールバック（2026-07-05 に公式ページで確認）
MPM_FALLBACK = {
    2026: [
        ("2026-01-22", "2026-01-23"),
        ("2026-03-18", "2026-03-19"),
        ("2026-04-27", "2026-04-28"),
        ("2026-06-15", "2026-06-16"),
        ("2026-07-30", "2026-07-31"),
        ("2026-09-17", "2026-09-18"),
        ("2026-10-29", "2026-10-30"),
        ("2026-12-17", "2026-12-18"),
    ],
}

# RSS から拾う「株式市場影響の大きい」発表のキーワード
RSS_KEYWORDS = [
    "金融政策決定会合", "声明", "展望レポート", "総裁", "記者会見",
    "政策金利", "短観", "基調的なインフレ", "需給ギャップ",
    "国債買入", "国債の買入れ", "ETF", "REIT", "オペレーション",
    "金融システムレポート", "さくらレポート", "主な意見", "議事要旨",
    "マネタリーベース", "生活意識",
]
RSS_MAX_ITEMS = 12
RSS_MAX_AGE_DAYS = 14

TANKAN_ROWS = {
    "製造業": "manufacturing",
    "非製造業": "nonmanufacturing",
    "全産業": "all_industries",
}
# 計表1 の列オフセット: (最近, 変化幅, 先行き)。前回調査は (最近, 先行き) のみ
TANKAN_COLS = {
    "large": {"prev": (2, 3), "cur": (4, 5, 6)},
    "small": {"prev": (14, 15), "cur": (16, 17, 18)},
}


def fetch_mpm_schedule() -> dict:
    """金融政策決定会合の年間日程を取得する（失敗時はフォールバック）。"""
    year = datetime.now(JST).year
    try:
        resp = requests.get(MPM_SCHEDULE_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text
        # 「1月22日（木）・23日（金）」等の表記を年ごとのセクションから拾う
        meetings = []
        year_m = re.search(rf"{year}\s*年", html)
        section = html[year_m.start():year_m.start() + 20000] if year_m else html
        for m in re.finditer(
                r"(\d{1,2})月(\d{1,2})日（[月火水木金土日]）"
                r"(?:・(?:(\d{1,2})月)?(\d{1,2})日（[月火水木金土日]）)?", section):
            mo1, d1 = int(m.group(1)), int(m.group(2))
            mo2 = int(m.group(3)) if m.group(3) else mo1
            d2 = int(m.group(4)) if m.group(4) else d1
            meetings.append((date(year, mo1, d1).isoformat(),
                             date(year, mo2, d2).isoformat()))
        # 重複除去・ソート。年8回が正常値なので極端に外れたら失敗扱い
        meetings = sorted(set(meetings))
        if not 4 <= len(meetings) <= 12:
            raise ValueError(f"会合日程のパース結果が異常: {len(meetings)} 件")
        note = "公式ページからスクレイプ"
    except Exception as e:
        meetings = MPM_FALLBACK.get(year, [])
        note = f"スクレイプ失敗（{type(e).__name__}）のためフォールバック日程を使用"
        if not meetings:
            raise RuntimeError(f"{year} 年の会合日程が取得できません: {e}")
    return {
        "year": year,
        "meetings": [{"start": s, "end": e} for s, e in meetings],
        "source_url": MPM_SCHEDULE_URL,
        "note": note,
    }


def tankan_publication(survey_year: int, survey_month: int) -> "tuple[str, str]":
    """調査月から (公表日, 利用可能日) を推定する（3/6/9月→翌月1日、12月→12/14頃）。"""
    if survey_month == 12:
        pub = date(survey_year, 12, 14)
    else:
        pub = date(survey_year, survey_month + 1, 1)
    return pub.isoformat(), (pub + timedelta(days=1)).isoformat()


def parse_tankan_sheet(xls_bytes: bytes) -> "list[dict]":
    """計表1 から今回・前回調査の業況判断 DI を抽出する。"""
    df = pd.ExcelFile(io.BytesIO(xls_bytes)).parse("計表1", header=None)

    # ヘッダー行から調査年月を特定（例: "2026年6月調査"）
    surveys = {}  # {"prev": (y, m), "cur": (y, m)}
    for i in range(min(20, len(df))):
        for j in range(min(8, df.shape[1])):
            v = str(df.iloc[i, j])
            m = re.search(r"(\d{4})年(\d{1,2})月調査", v)
            if m:
                key = "prev" if j <= 3 else "cur"
                surveys.setdefault(key, (int(m.group(1)), int(m.group(2))))
    if "cur" not in surveys or "prev" not in surveys:
        raise RuntimeError("計表1 から調査年月を特定できません（レイアウト変更の可能性）")

    # 各ラベルの最初の出現行を業況判断 DI とみなす
    rows = {}
    for i in range(len(df)):
        label = str(df.iloc[i, 0]).strip()
        if label in TANKAN_ROWS and TANKAN_ROWS[label] not in rows:
            rows[TANKAN_ROWS[label]] = i
    if "manufacturing" not in rows:
        raise RuntimeError("計表1 に業況判断 DI の行が見つかりません")

    def _num(i, j):
        try:
            v = float(df.iloc[i, j])
            return round(v, 1)
        except (TypeError, ValueError):
            return None

    results = []
    for key, (sy, sm) in (("prev", surveys["prev"]), ("cur", surveys["cur"])):
        pub, avail = tankan_publication(sy, sm)
        di = {}
        for sector, i in rows.items():
            for size, cols in TANKAN_COLS.items():
                if key == "cur":
                    c_cur, c_chg, c_fct = cols["cur"]
                    di[f"{size}_{sector}"] = {
                        "current_di": _num(i, c_cur),
                        "change_from_prev": _num(i, c_chg),
                        "forecast_di": _num(i, c_fct),
                    }
                else:
                    c_cur, c_fct = cols["prev"]
                    di[f"{size}_{sector}"] = {
                        "current_di": _num(i, c_cur),
                        "forecast_di": _num(i, c_fct),
                    }
        results.append({
            "survey": f"{sy:04d}-{sm:02d}",
            "published": pub,
            "available_from": avail,
            "di": di,
        })
    return results


def fetch_tankan() -> dict:
    """直近2回分の短観概要 ZIP を取得し、4調査分の DI を組み立てる。"""
    now = datetime.now(JST)
    # 直近の調査四半期（3/6/9/12月）から新しい順に候補を作る
    candidates = []
    y, q = now.year, ((now.month - 1) // 3) * 3 + 3  # 今quarterの調査月
    for _ in range(6):
        candidates.append((y, q))
        q -= 3
        if q == 0:
            y, q = y - 1, 12
    surveys, sources, fetched = {}, [], 0
    for sy, sm in candidates:
        if fetched >= 2:
            break
        url = TANKAN_ZIP_TEMPLATE.format(year=sy, yy=sy % 100, mm=sm)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            continue
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xlsx_names = [n for n in zf.namelist() if n.endswith(".xlsx")]
            if not xlsx_names:
                continue
            for rec in parse_tankan_sheet(zf.read(xlsx_names[0])):
                surveys.setdefault(rec["survey"], rec)
        sources.append(url)
        fetched += 1
    if not surveys:
        raise RuntimeError("短観の概要 ZIP が取得できません")
    return {
        "surveys": sorted(surveys.values(), key=lambda r: r["survey"]),
        "source_urls": sources,
        "note": ("業況判断DI（良い-悪い、%ポイント）。計表1の"
                 "大企業(large)・中小企業(small) × 製造業/非製造業/全産業。"
                 "公表日は 3/6/9月調査=翌月1日・12月調査=12/14 の推定値"),
    }


def fetch_announcements() -> "list[dict]":
    """What's New RSS から株式市場影響の大きい発表を抽出する。"""
    feed = feedparser.parse(RSS_URL)
    cutoff = datetime.now(JST) - timedelta(days=RSS_MAX_AGE_DAYS)
    items = []
    for e in feed.entries:
        title = e.get("title", "")
        if not any(k in title for k in RSS_KEYWORDS):
            continue
        published = None
        if e.get("published_parsed"):
            published = datetime(*e.published_parsed[:6]).strftime("%Y-%m-%d")
            if datetime(*e.published_parsed[:6], tzinfo=JST) < cutoff:
                continue
        items.append({"title": title, "published": published,
                      "link": e.get("link")})
        if len(items) >= RSS_MAX_ITEMS:
            break
    return items


def main() -> dict:
    with Timer() as t:
        try:
            out = {
                "source": "日本銀行（金融政策決定会合日程・短観・新着発表）",
                "fetched_at": now_jst_iso(),
                "mpm_schedule": fetch_mpm_schedule(),
                "tankan": fetch_tankan(),
                "announcements": {
                    "source_url": RSS_URL,
                    "note": (f"新着RSSから重要キーワード該当のみ抽出"
                             f"（直近{RSS_MAX_AGE_DAYS}日・最大{RSS_MAX_ITEMS}件）。"
                             "過去分の遡及は不可"),
                    "items": fetch_announcements(),
                },
            }
            path = save_json("macro_boj.json", out)
            n_meet = len(out["mpm_schedule"]["meetings"])
            n_tk = len(out["tankan"]["surveys"])
            n_ann = len(out["announcements"]["items"])
            note = f"会合{n_meet}回 / 短観{n_tk}調査 / 発表{n_ann}件 -> {path.name}"
            ok, count = True, n_meet + n_tk + n_ann
        except Exception as e:
            ok, count, note = False, 0, f"{type(e).__name__}: {e}"
    print_summary(SOURCE, ok, count, t.seconds, note)
    return {"source": SOURCE, "ok": ok, "count": count,
            "seconds": t.seconds, "note": note, "fetched_at": now_jst_iso()}


if __name__ == "__main__":
    sys.exit(0 if main()["ok"] else 1)
