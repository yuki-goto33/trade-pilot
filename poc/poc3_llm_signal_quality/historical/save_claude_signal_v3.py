"""Claude 代行生成用（v3）: 専門家見解2件+最終シグナルを検証して保存する。

export_pending_contexts_v3.py のタスクに対する応答を、run_historical --mode experts
と同一のレコード形式で data/signals_historical_v3/ に保存する。
検証エラー時は非 0 で終了（エージェントは修正して再実行）。

使い方:
    ../../../.venv/bin/python save_claude_signal_v3.py 2026-04-02 7203 \
        /path/tech.json /path/fund.json /path/signal.json
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jsonschema

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
sys.path.insert(0, str(POC3_DIR))
sys.path.insert(0, str(HIST_DIR))

from experts import validate_expert_view  # noqa: E402
from generate_signal import parse_response, validate_signal  # noqa: E402
from export_pending_contexts_v3 import PENDING_DIR, SIGNALS_DIR_V3  # noqa: E402
from run_historical import signal_path  # noqa: E402

JST = timezone(timedelta(hours=9))
CLAUDE_MODEL = "claude-sonnet-4-6"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asof")
    parser.add_argument("code")
    parser.add_argument("tech_json", type=Path)
    parser.add_argument("fund_json", type=Path)
    parser.add_argument("signal_json", type=Path)
    parser.add_argument("--model", default=CLAUDE_MODEL)
    args = parser.parse_args()

    pending = PENDING_DIR / f"{args.asof}_{args.code}.json"
    if not pending.exists():
        sys.exit(f"pending ファイルがありません: {pending}")
    with open(pending, encoding="utf-8") as f:
        task = json.load(f)

    try:
        tech = parse_response(args.tech_json.read_text(encoding="utf-8"))
        validate_expert_view(tech)
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        print(f"検証エラー (technical): {e}", file=sys.stderr)
        sys.exit(1)
    try:
        fund = parse_response(args.fund_json.read_text(encoding="utf-8"))
        validate_expert_view(fund)
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        print(f"検証エラー (fundamental): {e}", file=sys.stderr)
        sys.exit(1)
    raw = args.signal_json.read_text(encoding="utf-8")
    try:
        signal = parse_response(raw)
        validate_signal(signal)
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        print(f"検証エラー (signal): {e}", file=sys.stderr)
        sys.exit(1)

    asof = date.fromisoformat(args.asof)
    path = signal_path(SIGNALS_DIR_V3, asof, args.code)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "code": args.code,
        "name": task["name"],
        "asof": args.asof,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "generator": f"historical (run_historical.py, {args.model})",
        "generated_by": "claude-code-agent (v3 experts)",
        "price_as_of": task["price_as_of"],
        "signal": signal,
        "expert_views": {"technical": tech, "fundamental": fund},
        "raw_response": raw,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"[OK ] {args.asof} {args.code}: {signal['signal']}"
          f" (confidence={signal['confidence']},"
          f" T:{tech['stance']}/F:{fund['stance']}) -> {path}")


if __name__ == "__main__":
    main()
