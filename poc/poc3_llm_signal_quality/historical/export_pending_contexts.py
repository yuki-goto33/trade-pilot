"""Claude 代行生成用: 未生成の (as-of, code) のプロンプトを書き出す。

Gemini 無料枠のクォータ枯渇で run_historical.py が進まない場合に、
Claude Code のサブエージェントに同一プロンプトでシグナル生成を代行させる。
その入力（system / user プロンプト）をファイルに書き出すのがこのスクリプト。

- system プロンプトは全銘柄共通のため data/claude_pending/system.md に 1 回だけ保存
- 各タスクは data/claude_pending/<asof>_<code>.json
  （asof, code, name, price_as_of, user プロンプト全文）
- 一覧を data/claude_pending/manifest.json に保存

使い方:
    ../../../.venv/bin/python export_pending_contexts.py --start 2026-04-01 --end 2026-06-30
"""
import argparse
import json
import sys
from pathlib import Path

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
sys.path.insert(0, str(POC3_DIR))
sys.path.insert(0, str(HIST_DIR))

from context_builder import ContextBuildError  # noqa: E402
from generate_signal import render_prompts  # noqa: E402
from historical_context import (  # noqa: E402
    DATA_DIR,
    UNIVERSE,
    build_context_asof,
    trading_dates,
)
from run_historical import DEFAULT_SIGNALS_DIR, signal_path  # noqa: E402

PENDING_DIR = DATA_DIR / "claude_pending"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="終了日 YYYY-MM-DD（含む）")
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR)
    args = parser.parse_args()

    codes = [s["code"] for s in UNIVERSE]
    tasks = []
    for code in codes:
        for d in trading_dates(code, args.start, args.end):
            tasks.append((d, code))
    tasks.sort()
    todo = [(d, c) for d, c in tasks
            if not signal_path(args.signals_dir, d, c).exists()]
    print(f"対象 {len(tasks)} / 生成済み {len(tasks) - len(todo)} / 書き出し {len(todo)}")

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    manifest, ng = [], 0
    system_written = False
    for asof, code in todo:
        try:
            context = build_context_asof(code, asof)
        except ContextBuildError as e:
            ng += 1
            print(f"[NG ] {asof} {code}: {e}", file=sys.stderr)
            continue
        system, user = render_prompts(context)
        if not system_written:
            (PENDING_DIR / "system.md").write_text(system, encoding="utf-8")
            system_written = True
        item = {
            "asof": asof.isoformat(),
            "code": code,
            "name": context["meta"]["name"],
            "price_as_of": context["price_technical"]["as_of"],
            "user": user,
        }
        path = PENDING_DIR / f"{asof.isoformat()}_{code}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False, indent=2)
        manifest.append(path.name)

    with open(PENDING_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {len(manifest)} 件 -> {PENDING_DIR}（NG {ng}）")


if __name__ == "__main__":
    main()
