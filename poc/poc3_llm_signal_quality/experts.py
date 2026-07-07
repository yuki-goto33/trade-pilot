"""v3 専門家アーキテクチャ: テクニカル/ファンダ専門家 + チーフアナリスト統合。

1銘柄あたり 3 回の LLM 呼び出しで最終シグナルを生成する:
1. テクニカル専門家   : price_technical + us_overnight + 市場スナップショット
2. ファンダ専門家     : fundamentals + news + disclosures + マクロ指標
3. チーフアナリスト   : 両専門家の見解 + 参照データ → 最終シグナル
   （出力は既存の signal_schema.json — バックテスト・レポートとの互換を維持）

コンテキストは build_context / build_context_asof の出力（同一構造）を
split_context() で分割するため、フォワード・ヒストリカルの両方で使える。
"""
import json
from pathlib import Path

import jsonschema

POC_DIR = Path(__file__).resolve().parent

EXPERT_SCHEMA_PATH = POC_DIR / "expert_view_schema.json"
TEMPLATES = {
    "technical": POC_DIR / "prompt_technical.md",
    "fundamental": POC_DIR / "prompt_fundamental.md",
    "synthesis": POC_DIR / "prompt_synthesis.md",
}

# コンテキストの macro キーの振り分け
# v5: us_market_overnight（NYフルグリッド）は廃止（銘柄別 us_overnight に一本化）。
# ドル円はテクニカル側の引用率1.6%（ファンダ側47.9%）のためファンダ専用にする。
TECH_MACRO_KEYS = ("indices", "market_regime")
FUND_MACRO_KEYS = ("indices", "market_regime", "rates",
                   "jfc_sme_survey", "boj", "japan_pmi", "japan_cpi")
TECH_EXCLUDE_INDEX_TICKERS = {"JPY=X"}


def load_expert_schema() -> dict:
    with open(EXPERT_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_sections(path: Path) -> dict:
    """プロンプト md から `## system` / `## user` セクションを抽出する。"""
    sections, current, buf = {}, None, []
    for line in path.read_text(encoding="utf-8").splitlines():
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
            raise ValueError(f"{path.name} に `## {key}` セクションがありません。")
    return sections


def split_context(context: dict) -> "tuple[dict, dict, dict]":
    """フルコンテキストを (テクニカル用, ファンダ用, 参照データ) に分割する。"""
    macro = context.get("macro") or {}
    tech_macro = {k: macro[k] for k in TECH_MACRO_KEYS if k in macro}
    if isinstance(tech_macro.get("indices"), list):
        tech_macro["indices"] = [
            i for i in tech_macro["indices"]
            if i.get("ticker") not in TECH_EXCLUDE_INDEX_TICKERS]
    tech = {
        "meta": context["meta"],
        "price_technical": context["price_technical"],
        "macro": tech_macro,
    }
    if context.get("us_overnight"):
        tech["us_overnight"] = context["us_overnight"]
    fund = {
        "meta": context["meta"],
        "fundamentals": context.get("fundamentals"),
        "news": context.get("news"),
        "disclosures": context.get("disclosures"),
        "macro": {k: macro[k] for k in FUND_MACRO_KEYS if k in macro},
    }
    pt = context.get("price_technical") or {}
    reference = {
        "as_of": pt.get("as_of"),
        "last_close": pt.get("last_close"),
        "period_high": pt.get("period_high"),
        "period_low": pt.get("period_low"),
        "sma25": pt.get("sma25"),
        "market_regime": (macro.get("market_regime") or {}).get("market_regime"),
        "next_earnings_date": (context.get("fundamentals") or {}).get(
            "next_earnings_date"),
        "next_boj_meeting": ((macro.get("boj") or {}).get(
            "monetary_policy_meeting") or {}).get("next_meeting"),
    }
    # v5: 直近14日以内の buy（フォワードのみ generate_signal が付与）。
    # 存在する場合、チーフアナリストは新規 buy を抑制する
    if context.get("recent_buy"):
        reference["recent_buy"] = context["recent_buy"]
    return tech, fund, reference


def render_expert_prompt(kind: str, context_part: dict, meta: dict,
                         as_of: str) -> "tuple[str, str]":
    """専門家（technical / fundamental）のプロンプトを組み立てる。"""
    sections = _load_sections(TEMPLATES[kind])
    schema_json = json.dumps(load_expert_schema(), ensure_ascii=False, indent=1)
    system = sections["system"].replace("{schema_json}", schema_json)
    user = (sections["user"]
            .replace("{stock_name}", meta["name"])
            .replace("{stock_code}", meta["code"])
            .replace("{as_of}", as_of or "")
            .replace("{context_json}",
                     json.dumps(context_part, ensure_ascii=False, indent=1)))
    return system, user


def render_synthesis_prompt(signal_schema: dict, tech_view: dict, fund_view: dict,
                            reference: dict, meta: dict,
                            as_of: str) -> "tuple[str, str]":
    """チーフアナリスト（統合）のプロンプトを組み立てる。"""
    sections = _load_sections(TEMPLATES["synthesis"])
    system = sections["system"].replace(
        "{schema_json}", json.dumps(signal_schema, ensure_ascii=False, indent=1))
    user = (sections["user"]
            .replace("{stock_name}", meta["name"])
            .replace("{stock_code}", meta["code"])
            .replace("{as_of}", as_of or "")
            .replace("{technical_view_json}",
                     json.dumps(tech_view, ensure_ascii=False, indent=1))
            .replace("{fundamental_view_json}",
                     json.dumps(fund_view, ensure_ascii=False, indent=1))
            .replace("{reference_json}",
                     json.dumps(reference, ensure_ascii=False, indent=1)))
    return system, user


def validate_expert_view(view: dict) -> None:
    jsonschema.validate(instance=view, schema=load_expert_schema())


def run_expert_pipeline(client, context: dict, parse_response, validate_signal,
                        log=None) -> "tuple[dict, dict, dict]":
    """3段パイプラインを実行し (最終シグナル, 専門家見解, 生応答) を返す。

    Args:
        client: LLMClient（complete(system, user) を持つ）
        context: build_context / build_context_asof の出力
        parse_response / validate_signal: generate_signal.py の関数（循環 import 回避）
        log: 進捗コールバック（str を受ける）。None なら無出力

    Raises:
        json.JSONDecodeError / jsonschema.ValidationError: 再サンプル1回でも
        スキーマ違反が解消しない場合（呼び出し側で NG 処理）
    """
    meta = context["meta"]
    as_of = (context.get("price_technical") or {}).get("as_of", "")
    tech_ctx, fund_ctx, reference = split_context(context)

    views, raws = {}, {}
    for kind, ctx_part in (("technical", tech_ctx), ("fundamental", fund_ctx)):
        system, user = render_expert_prompt(kind, ctx_part, meta, as_of)
        for retry in range(2):
            raw = client.complete(system, user)
            try:
                view = parse_response(raw)
                validate_expert_view(view)
                break
            except (json.JSONDecodeError, jsonschema.ValidationError):
                if retry == 1:
                    raise
                if log:
                    log(f"    [resample] {meta['code']} {kind}: スキーマ違反のため再生成")
        views[kind] = view
        raws[kind] = raw
        if log:
            log(f"    [{kind[:4]}] {meta['code']}: {view['stance']}"
                f" (strength={view['strength']})")

    from generate_signal import load_schema  # 遅延 import（循環回避）
    system, user = render_synthesis_prompt(
        load_schema(), views["technical"], views["fundamental"],
        reference, meta, as_of)
    for retry in range(2):
        raw = client.complete(system, user)
        try:
            signal = parse_response(raw)
            validate_signal(signal)
            break
        except (json.JSONDecodeError, jsonschema.ValidationError):
            if retry == 1:
                raise
            if log:
                log(f"    [resample] {meta['code']} synthesis: スキーマ違反のため再生成")
    raws["synthesis"] = raw
    return signal, views, raws
