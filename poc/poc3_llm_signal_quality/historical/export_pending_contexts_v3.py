"""Claude 代行生成用（v3 専門家アーキテクチャ）: プロンプト一式を書き出す。

v3 は銘柄×日付ごとに「テクニカル専門家 → ファンダ専門家 → チーフアナリスト統合」
の3段の LLM 呼び出しが必要。Claude Code のサブエージェントに代行させるため、
以下を書き出す:

- data/claude_pending_v3/system_technical.md / system_fundamental.md /
  system_synthesis.md : 3役の共通システムプロンプト（スキーマ埋め込み済み）
- data/claude_pending_v3/<asof>_<code>.json :
  {asof, code, name, price_as_of, technical_user, fundamental_user, reference}
- data/claude_pending_v3/manifest.json / batches/batch_NN.txt

使い方:
    ../../../.venv/bin/python export_pending_contexts_v3.py \
        --start 2026-04-01 --end 2026-05-31 --batch-size 10
"""
import argparse
import json
import os
import math
import sys
from pathlib import Path

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
sys.path.insert(0, str(POC3_DIR))
sys.path.insert(0, str(HIST_DIR))

from context_builder import ContextBuildError  # noqa: E402
from experts import (  # noqa: E402
    _load_sections,
    TEMPLATES,
    load_expert_schema,
    render_expert_prompt,
    split_context,
)
from generate_signal import load_schema  # noqa: E402
from historical_context import (  # noqa: E402
    DATA_DIR,
    UNIVERSE,
    build_context_asof,
    trading_dates,
)
from run_historical import signal_path  # noqa: E402

# 環境変数 TP_HIST_TAG で世代を切替（v3 / v4 ...）。pending / signals ディレクトリが対で変わる
TAG = os.environ.get("TP_HIST_TAG", "v3")
PENDING_DIR = DATA_DIR / f"claude_pending_{TAG}"
SIGNALS_DIR_V3 = DATA_DIR / f"signals_historical_{TAG}"


def write_system_prompts() -> None:
    schema_json = json.dumps(load_expert_schema(), ensure_ascii=False, indent=1)
    for kind in ("technical", "fundamental"):
        system = _load_sections(TEMPLATES[kind])["system"].replace(
            "{schema_json}", schema_json)
        (PENDING_DIR / f"system_{kind}.md").write_text(system, encoding="utf-8")
    synth = _load_sections(TEMPLATES["synthesis"])["system"].replace(
        "{schema_json}", json.dumps(load_schema(), ensure_ascii=False, indent=1))
    (PENDING_DIR / "system_synthesis.md").write_text(synth, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()

    codes = [s["code"] for s in UNIVERSE]
    tasks = []
    for code in codes:
        for d in trading_dates(code, args.start, args.end):
            tasks.append((d, code))
    tasks.sort()
    todo = [(d, c) for d, c in tasks
            if not signal_path(SIGNALS_DIR_V3, d, c).exists()]
    print(f"対象 {len(tasks)} / 生成済み {len(tasks) - len(todo)} / 書き出し {len(todo)}")

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    write_system_prompts()

    manifest, ng = [], 0
    for asof, code in todo:
        try:
            context = build_context_asof(code, asof)
        except ContextBuildError as e:
            ng += 1
            print(f"[NG ] {asof} {code}: {e}", file=sys.stderr)
            continue
        meta = context["meta"]
        as_of = context["price_technical"]["as_of"]
        tech_ctx, fund_ctx, reference = split_context(context)
        _, tech_user = render_expert_prompt("technical", tech_ctx, meta, as_of)
        _, fund_user = render_expert_prompt("fundamental", fund_ctx, meta, as_of)
        item = {
            "asof": asof.isoformat(),
            "code": code,
            "name": meta["name"],
            "price_as_of": as_of,
            "technical_user": tech_user,
            "fundamental_user": fund_user,
            "reference": reference,
        }
        path = PENDING_DIR / f"{asof.isoformat()}_{code}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False, indent=1)
        manifest.append(path.name)

    with open(PENDING_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    bdir = PENDING_DIR / "batches"
    bdir.mkdir(exist_ok=True)
    for f_old in bdir.glob("batch_*.txt"):
        f_old.unlink()
    nb = math.ceil(len(manifest) / args.batch_size)
    for i in range(nb):
        chunk = manifest[i * args.batch_size:(i + 1) * args.batch_size]
        lines = []
        for name in chunk:
            asof, code = name[:-5].split("_")
            lines.append(f"{asof} {code} {name}")
        (bdir / f"batch_{i+1:02d}.txt").write_text("\n".join(lines) + "\n",
                                                   encoding="utf-8")
    print(f"書き出し完了: {len(manifest)} 件 / バッチ {nb} 個（{args.batch_size}件/個）"
          f" -> {PENDING_DIR}（NG {ng}）")


if __name__ == "__main__":
    main()
