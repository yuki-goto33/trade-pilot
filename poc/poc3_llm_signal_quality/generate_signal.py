"""PoC-3: LLM シグナル生成パイプライン。

流れ: context_builder → プロンプト組み立て → LLM 呼び出し（llm_client 経由）
      → JSON パース・スキーマ検証 → data/signals/ に保存

LLM プロバイダは未選定のため、現状は stub（呼ぶと NotImplementedError）。
`--dry-run` を付けると LLM を呼ばずに「実際に送るプロンプト全文」を
data/signals/dry_run/ にファイル出力し、トークン量の概算を表示する。

使い方:
    # universe 全銘柄の dry-run（プロンプト出力 + トークン見積もり）
    ../../.venv/bin/python generate_signal.py --dry-run

    # 特定銘柄のみ
    ../../.venv/bin/python generate_signal.py --dry-run --codes 7203 6758

    # 本実行（プロバイダ実装後）
    ../../.venv/bin/python generate_signal.py --provider <name>
"""
import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jsonschema

from context_builder import (
    DATA_DIR,
    REPO_ROOT,
    UNIVERSE,
    ContextBuildError,
    build_context,
    context_to_json,
)
from llm_client import get_client

POC_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = POC_DIR / "signal_schema.json"
TEMPLATE_PATH = POC_DIR / "prompt_template.md"
SIGNALS_DIR = DATA_DIR / "signals"

# フォワードの buy 履歴の検索先。data/signals はローカル実行の蓄積、
# signals_history は git 管理下（GitHub Actions のチェックアウトで過去分が入る）
SIGNALS_HISTORY_DIR = REPO_ROOT / "poc" / "poc5_daily_report" / "signals_history"

# v5: 同一銘柄への再 buy を抑制する日数
REBUY_SUPPRESS_DAYS = 14

JST = timezone(timedelta(hours=9))

# 出力トークンの想定値（スキーマ準拠の応答 JSON 1件分の概算。README 参照）
ASSUMED_OUTPUT_TOKENS_PER_STOCK = 500

# 月間見積もりの前提
MONTHLY_STOCKS = 50
MONTHLY_TRADING_DAYS = 22


# ---------------------------------------------------------------------------
# プロンプト組み立て
# ---------------------------------------------------------------------------

def load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_template_sections() -> dict:
    """prompt_template.md から `## system` / `## user` セクションを抽出する。"""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    sections = {}
    current = None
    buf = []
    for line in text.splitlines():
        heading = line.strip().lower()
        if heading in ("## system", "## user"):
            if current:
                sections[current] = "\n".join(buf).strip()
            current = heading[3:]
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    for key in ("system", "user"):
        if key not in sections:
            raise ValueError(f"prompt_template.md に `## {key}` セクションがありません。")
    return sections


def render_prompts(context: dict) -> "tuple[str, str]":
    """コンテキストからシステム/ユーザープロンプトを組み立てる。"""
    sections = load_template_sections()
    schema_json = json.dumps(load_schema(), ensure_ascii=False, indent=1)
    system = sections["system"].replace("{schema_json}", schema_json)
    user = (
        sections["user"]
        .replace("{stock_name}", context["meta"]["name"])
        .replace("{stock_code}", context["meta"]["code"])
        .replace("{as_of}", context["price_technical"]["as_of"])
        .replace("{context_json}", context_to_json(context))
    )
    return system, user


# ---------------------------------------------------------------------------
# トークン概算
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """粗いトークン概算: 日本語等の非 ASCII は 1文字=1トークン、ASCII は 4文字=1トークン。"""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return round(ascii_chars / 4) + non_ascii_chars


# ---------------------------------------------------------------------------
# LLM 応答の処理
# ---------------------------------------------------------------------------

def parse_response(raw: str) -> dict:
    """LLM 応答テキストから JSON を取り出してパースする。

    指示上コードフェンスは禁止だが、防御的に ```json フェンスは剥がす。
    """
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def validate_signal(signal: dict) -> None:
    """signal_schema.json に対してスキーマ検証する（違反時は ValidationError）。"""
    jsonschema.validate(instance=signal, schema=load_schema())


def find_recent_buy(code: str, days: int = REBUY_SUPPRESS_DAYS) -> "dict | None":
    """直近 days 日以内（当日を除く）に当該銘柄へ buy を出していれば返す。

    data/signals（ローカル蓄積）と poc5 signals_history（git 管理下）の
    両方を走査する。GitHub Actions ではチェックアウトされた signals_history
    が唯一の過去履歴になる。
    """
    today = datetime.now(JST).date()
    latest = None
    for base in (SIGNALS_DIR, SIGNALS_HISTORY_DIR):
        if not base.is_dir():
            continue
        for day_dir in base.iterdir():
            try:
                d = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if not (0 < (today - d).days <= days):
                continue
            path = day_dir / f"{code}.json"
            if not path.is_file():
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            sig = rec.get("signal") or {}
            if sig.get("signal") == "buy" and (latest is None or d > latest[0]):
                latest = (d, sig.get("confidence"))
    if latest is None:
        return None
    return {
        "date": latest[0].isoformat(),
        "days_ago": (today - latest[0]).days,
        "confidence": latest[1],
        "note": (f"直近{REBUY_SUPPRESS_DAYS}日以内に buy シグナル済み。"
                 "新規 buy は出さず hold（継続監視）とすること"),
    }


def build_context_refs(code: str) -> dict:
    """レポート引用用のニュース・開示リンク（LLM プロンプトには含めない）。

    build_news / build_disclosures はトークン節約のため URL を落とすので、
    元 JSON から URL 付きで拾い直してシグナルレコードに保存する。
    """
    refs = {}
    try:
        with open(DATA_DIR / "news_google.json", encoding="utf-8") as f:
            data = json.load(f)
        items = ((data.get("news") or {}).get(code) or {}).get("items", [])[:10]
        refs["news"] = [
            {"title": i.get("title"), "url": i.get("link"),
             "published": i.get("published"), "publisher": i.get("source")}
            for i in items
        ]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        with open(DATA_DIR / "disclosures_yanoshin.json", encoding="utf-8") as f:
            data = json.load(f)
        items = ((data.get("disclosures") or {}).get(code) or {}).get("items", [])[:10]
        refs["disclosures"] = [
            {"title": i.get("title"), "url": i.get("document_url"),
             "date": i.get("pubdate")}
            for i in items
        ]
    except (OSError, json.JSONDecodeError):
        pass
    return refs


def save_signal(code: str, name: str, context: dict, signal: dict, raw: str,
                expert_views: dict = None) -> Path:
    """検証済みシグナルを data/signals/<日付>/<code>.json に保存する。"""
    now = datetime.now(JST)
    out_dir = SIGNALS_DIR / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{code}.json"
    record = {
        "code": code,
        "name": name,
        "generated_at": now.isoformat(timespec="seconds"),
        "price_as_of": context["price_technical"]["as_of"],
        "signal": signal,
        "raw_response": raw,
    }
    if expert_views:
        record["expert_views"] = expert_views
    if context.get("recent_buy"):
        record["recent_buy"] = context["recent_buy"]
    refs = build_context_refs(code)
    if refs:
        record["context_refs"] = refs
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# パイプライン
# ---------------------------------------------------------------------------

def run_dry_run(codes: "list[str]") -> None:
    """LLM を呼ばず、送信予定のプロンプト全文とトークン概算を出力する。"""
    out_dir = SIGNALS_DIR / "dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for code in codes:
        try:
            context = build_context(code)
        except ContextBuildError as e:
            print(f"[SKIP] {code}: {e}", file=sys.stderr)
            continue
        system, user = render_prompts(context)

        prompt_path = out_dir / f"{code}_prompt.txt"
        prompt_path.write_text(
            "=== SYSTEM PROMPT ===\n\n"
            + system
            + "\n\n=== USER PROMPT ===\n\n"
            + user
            + "\n",
            encoding="utf-8",
        )

        rows.append(
            {
                "code": code,
                "name": context["meta"]["name"],
                "system_chars": len(system),
                "user_chars": len(user),
                "system_tokens_est": estimate_tokens(system),
                "user_tokens_est": estimate_tokens(user),
                "input_tokens_est": estimate_tokens(system) + estimate_tokens(user),
                "prompt_file": str(prompt_path.relative_to(DATA_DIR.parent)),
            }
        )

    if not rows:
        print("有効な銘柄がありませんでした。", file=sys.stderr)
        sys.exit(1)

    # 集計と月間見積もり
    avg_input = round(sum(r["input_tokens_est"] for r in rows) / len(rows))
    monthly_calls = MONTHLY_STOCKS * MONTHLY_TRADING_DAYS
    summary = {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "token_estimation_rule": "非ASCII 1文字=1トークン / ASCII 4文字=1トークン",
        "stocks": rows,
        "avg_input_tokens_per_stock": avg_input,
        "assumed_output_tokens_per_stock": ASSUMED_OUTPUT_TOKENS_PER_STOCK,
        "monthly_assumption": f"{MONTHLY_STOCKS}銘柄 × 日次 × {MONTHLY_TRADING_DAYS}営業日 = {monthly_calls}回",
        "monthly_input_tokens_est": avg_input * monthly_calls,
        "monthly_output_tokens_est": ASSUMED_OUTPUT_TOKENS_PER_STOCK * monthly_calls,
    }
    summary_path = out_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"{'code':>6} {'銘柄':<14} {'sys tok':>8} {'user tok':>9} {'input計':>8}")
    for r in rows:
        print(
            f"{r['code']:>6} {r['name'][:7]:<14} {r['system_tokens_est']:>8,}"
            f" {r['user_tokens_est']:>9,} {r['input_tokens_est']:>8,}"
        )
    print()
    print(f"平均入力トークン/銘柄: {avg_input:,}")
    print(f"想定出力トークン/銘柄: {ASSUMED_OUTPUT_TOKENS_PER_STOCK:,}")
    print(f"月間（{summary['monthly_assumption']}）:")
    print(f"  入力: {summary['monthly_input_tokens_est']:,} トークン")
    print(f"  出力: {summary['monthly_output_tokens_est']:,} トークン")
    print()
    print(f"プロンプト全文: {out_dir}/")
    print(f"集計: {summary_path}")


def run_generate(codes: "list[str]", provider: str, mode: str = "experts") -> None:
    """本実行: LLM を呼び、検証済みシグナルを保存する。

    mode="experts"（既定）はテクニカル/ファンダ専門家 + チーフアナリスト統合の
    3段パイプライン（銘柄あたり LLM 3回）。mode="single" は従来の一括判定。
    """
    from experts import run_expert_pipeline

    client = get_client(provider)
    ok, ng = 0, 0
    for code in codes:
        try:
            context = build_context(code)
            recent = find_recent_buy(code)
            if recent:
                context["recent_buy"] = recent
            expert_views = None
            if mode == "experts":
                signal, views, raws = run_expert_pipeline(
                    client, context, parse_response, validate_signal,
                    log=lambda m: print(m, file=sys.stderr))
                raw = raws["synthesis"]
                expert_views = views
            else:
                system, user = render_prompts(context)
                raw = client.complete(system, user)
                signal = parse_response(raw)
                validate_signal(signal)
            path = save_signal(code, context["meta"]["name"], context, signal,
                               raw, expert_views=expert_views)
            stance = ""
            if expert_views:
                stance = (f" [T:{expert_views['technical']['stance']}"
                          f"/F:{expert_views['fundamental']['stance']}]")
            print(f"[OK ] {code} {context['meta']['name']}: {signal['signal']}"
                  f" (confidence={signal['confidence']}){stance} -> {path}")
            ok += 1
        except NotImplementedError as e:
            print(f"[NG ] {code}: {e}", file=sys.stderr)
            print("--dry-run でプロンプト確認のみ行えます。", file=sys.stderr)
            sys.exit(2)
        except (ContextBuildError, json.JSONDecodeError,
                jsonschema.ValidationError) as e:
            print(f"[NG ] {code}: {type(e).__name__}: {e}", file=sys.stderr)
            ng += 1
    print(f"\n完了: OK {ok} / NG {ng}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PoC-3 LLM シグナル生成パイプライン")
    parser.add_argument(
        "--codes",
        nargs="*",
        default=None,
        help="対象銘柄コード（省略時は universe 全銘柄）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="LLM を呼ばず、送信予定のプロンプト全文とトークン概算を出力",
    )
    parser.add_argument(
        "--provider",
        default="stub",
        help="LLM プロバイダ名（現状 'stub' のみ。実装後に追加）",
    )
    parser.add_argument(
        "--mode",
        choices=["experts", "single"],
        default="experts",
        help="experts=2専門家+統合の3段パイプライン（既定） / single=従来の一括判定",
    )
    args = parser.parse_args()

    codes = args.codes or [s["code"] for s in UNIVERSE]
    unknown = [c for c in codes if c not in {s["code"] for s in UNIVERSE}]
    if unknown:
        parser.error(f"universe に存在しないコード: {unknown}")

    if args.dry_run:
        run_dry_run(codes)
    else:
        run_generate(codes, args.provider, mode=args.mode)


if __name__ == "__main__":
    main()
