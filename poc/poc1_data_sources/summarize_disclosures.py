"""重要開示（TDnet PDF）の中身を Gemini で要約し、キャッシュする。

シグナル生成のコンテキストには開示の「タイトル」しか入っておらず、
決算短信等の中身（業績数値・会社予想・株主還元）が使えない問題への対応。

流れ:
    1. data/disclosures_yanoshin.json から銘柄ごとに重要開示を選定
       - タイトルに 決算短信/業績予想/業績修正/配当/自己株式 のいずれかを含む
       - 訂正のみの開示・株式報酬系の自己株式処分はノイズとして除外
       - 直近90日以内、銘柄あたり最大2件（決算短信 > 業績修正 > … の優先順）
    2. 未要約の開示のみ PDF 取得（yanoshin 経由の TDnet 原本）
       → pypdf でテキスト抽出（先頭 ~8,000字）
       → GeminiClient で 3〜5 行に要約（レート制限対策は GeminiClient 任せ）
    3. data/disclosure_summaries.json にキャッシュ（キー: 開示 id）。
       既に要約済みの開示は再要約しない = 毎朝の増分だけ Gemini を呼ぶ。

出力: data/disclosure_summaries.json
"""
import io
import json
import re
import sys
from datetime import datetime, timedelta

import requests
from pypdf import PdfReader

from common import (
    DATA_DIR,
    JST,
    Timer,
    now_jst_iso,
    parse_universe_arg,
    print_summary,
    save_json,
)
from universe import load_universe

# GeminiClient（poc3）を共有する
sys.path.insert(0, str(DATA_DIR.parent / "poc" / "poc3_llm_signal_quality"))
from llm_client import GeminiClient  # noqa: E402

SOURCE = "disclosure_summaries"
IN_NAME = "disclosures_yanoshin.json"
OUT_NAME = "disclosure_summaries.json"

# 重要開示の選定条件
IMPORTANT_PAT = re.compile(r"決算短信|業績予想|業績修正|配当|自己株式")
EXCLUDE_PAT = re.compile(r"訂正|株式報酬")
PRIORITY_KEYWORDS = ["決算短信", "業績修正", "業績予想", "配当", "自己株式"]
RECENT_DAYS = 90
MAX_PER_STOCK = 2

PDF_TEXT_LIMIT = 8000
PDF_TIMEOUT_SEC = 60
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (trade-pilot PoC)"}

SUMMARY_SYSTEM_PROMPT = """\
あなたは日本株の適時開示（TDnet）を要約するアシスタントです。
与えられた開示 PDF のテキスト抜粋のみに基づいて、以下の観点を3〜5行の日本語で要約してください。

- 業績数値（売上高・営業利益・純利益など。数値には単位を付ける）
- 前年比・前期比の増減
- 会社予想（今期ガイダンス）
- 株主還元（配当・自己株式取得/消却）

厳守事項:
- 抜粋テキストに書かれていないことは書かない（推測・補完の禁止）
- 該当する情報が抜粋にない観点は省略してよい
- 出力は JSON オブジェクト {"summary": "要約テキスト"} のみ
"""


def _priority(title: str) -> int:
    for i, kw in enumerate(PRIORITY_KEYWORDS):
        if kw in title:
            return i
    return len(PRIORITY_KEYWORDS)


def select_important(disclosures: dict, codes: "list[str]") -> "list[dict]":
    """銘柄ごとに要約対象の重要開示を選定する。"""
    cutoff = (datetime.now(JST) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    selected = []
    for code in codes:
        items = (disclosures.get(code) or {}).get("items", [])
        candidates = [
            it for it in items
            if it.get("id") and it.get("document_url")
            and IMPORTANT_PAT.search(it.get("title", ""))
            and not EXCLUDE_PAT.search(it.get("title", ""))
            and (it.get("pubdate") or "") >= cutoff
        ]
        # 新しい順 → 安定ソートで優先度順（同優先度内は新しい順が保たれる）
        candidates.sort(key=lambda it: it["pubdate"], reverse=True)
        candidates.sort(key=lambda it: _priority(it["title"]))
        for it in candidates[:MAX_PER_STOCK]:
            selected.append({"code": code, **it})
    return selected


def fetch_pdf_text(url: str) -> str:
    """開示 PDF を取得してテキストを抽出する（先頭 PDF_TEXT_LIMIT 字）。"""
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=PDF_TIMEOUT_SEC)
    resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))
    chunks = []
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        chunks.append(text)
        total += len(text)
        if total >= PDF_TEXT_LIMIT:
            break
    return "".join(chunks)[:PDF_TEXT_LIMIT]


def summarize_one(client: GeminiClient, item: dict, pdf_text: str) -> str:
    """1件の開示テキストを Gemini で要約する。"""
    user = (
        f"銘柄: {item.get('company_name')}（証券コード: {item['code']}）\n"
        f"開示日: {item.get('pubdate')}\n"
        f"開示タイトル: {item.get('title')}\n\n"
        f"--- 開示 PDF テキスト抜粋（先頭 {PDF_TEXT_LIMIT} 字） ---\n"
        f"{pdf_text}"
    )
    raw = client.complete(SUMMARY_SYSTEM_PROMPT, user)
    try:
        return json.loads(raw)["summary"].strip()
    except (ValueError, KeyError, AttributeError):
        # JSON でなかった場合は生テキストを防御的に使う
        return raw.strip()[:600]


def load_cache() -> dict:
    path = DATA_DIR / OUT_NAME
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"updated_at": None, "summaries": {}}


def main(universe_path=None) -> dict:
    universe = load_universe(universe_path)
    codes = [s["code"] for s in universe]

    with Timer() as t:
        try:
            in_path = DATA_DIR / IN_NAME
            if not in_path.exists():
                raise FileNotFoundError(
                    f"{in_path} がありません。fetch_disclosures_yanoshin.py を先に実行してください。"
                )
            with open(in_path, encoding="utf-8") as f:
                disclosures = json.load(f).get("disclosures", {})

            selected = select_important(disclosures, codes)
            cache = load_cache()
            summaries = cache["summaries"]

            todo = [it for it in selected if str(it["id"]) not in summaries]
            print(f"  重要開示 {len(selected)} 件（新規要約 {len(todo)} 件 / "
                  f"キャッシュ済 {len(selected) - len(todo)} 件）")

            client = GeminiClient() if todo else None
            new_ok, failed = 0, 0
            for it in todo:
                key = str(it["id"])
                label = f"{it['code']} {it['pubdate'][:10]} {it['title'][:30]}"
                try:
                    pdf_text = fetch_pdf_text(it["document_url"])
                    if not pdf_text.strip():
                        raise ValueError("PDF からテキストを抽出できませんでした")
                    summary_text = summarize_one(client, it, pdf_text)
                    summaries[key] = {
                        "code": it["code"],
                        "pubdate": it["pubdate"],
                        "title": it["title"],
                        "document_url": it["document_url"],
                        "summary": summary_text,
                        "model": client.model,
                        "summarized_at": now_jst_iso(),
                    }
                    new_ok += 1
                    print(f"  [OK ] {label}")
                except requests.HTTPError as e:
                    failed += 1
                    print(f"  [NG ] {label}: {e}", file=sys.stderr)
                    if e.response is not None and e.response.status_code == 404:
                        # TDnet 原本は公表後 約1ヶ月で削除される。404 は恒久的な
                        # 欠損なので、翌日以降に再取得しないよう記録しておく
                        # （summary が無いエントリはコンテキストには載らない）
                        summaries[key] = {
                            "code": it["code"],
                            "pubdate": it["pubdate"],
                            "title": it["title"],
                            "document_url": it["document_url"],
                            "summary": None,
                            "error": "PDF 404（TDnet 原本の保存期間切れ）",
                            "summarized_at": now_jst_iso(),
                        }
                except Exception as e:  # noqa: BLE001 - 一時的な失敗はキャッシュせず翌日再試行
                    failed += 1
                    print(f"  [NG ] {label}: {type(e).__name__}: {e}", file=sys.stderr)

            cache["updated_at"] = now_jst_iso()
            out = save_json(OUT_NAME, cache)
            note = (f"重要開示 {len(selected)} 件, 新規要約 {new_ok} 件, "
                    f"失敗 {failed} 件, キャッシュ計 {len(summaries)} 件, 保存先 {out.name}")
            summary = {"source": SOURCE, "ok": True, "count": len(selected), "note": note}
        except Exception as e:  # noqa: BLE001
            summary = {"source": SOURCE, "ok": False, "count": 0,
                       "note": f"{type(e).__name__}: {e}"}
    summary["seconds"] = t.seconds
    summary["fetched_at"] = now_jst_iso()
    print_summary(SOURCE, summary["ok"], summary["count"], t.seconds, summary["note"])
    return summary


if __name__ == "__main__":
    sys.exit(0 if main(parse_universe_arg(__doc__))["ok"] else 1)
