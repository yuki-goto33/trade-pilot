"""Claude 代行生成用: エージェントが出力したシグナル JSON を検証して保存する。

export_pending_contexts.py が書き出したタスクに対する応答を、
run_historical.py と同一のレコード形式で signals ディレクトリに保存する。
スキーマ違反時は非 0 で終了しエラー内容を stderr に出す（エージェントは修正して再実行）。

使い方:
    ../../../.venv/bin/python save_claude_signal.py 2026-06-02 7203 /path/to/signal.json
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

from generate_signal import parse_response, validate_signal  # noqa: E402
from export_pending_contexts import PENDING_DIR  # noqa: E402
from run_historical import DEFAULT_SIGNALS_DIR, signal_path  # noqa: E402

JST = timezone(timedelta(hours=9))
CLAUDE_MODEL = "claude-sonnet-4-6"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asof", help="as-of 日 YYYY-MM-DD")
    parser.add_argument("code", help="銘柄コード")
    parser.add_argument("signal_json", type=Path, help="シグナル JSON ファイル")
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR)
    parser.add_argument("--model", default=CLAUDE_MODEL)
    args = parser.parse_args()

    pending = PENDING_DIR / f"{args.asof}_{args.code}.json"
    if not pending.exists():
        sys.exit(f"pending ファイルがありません: {pending}")
    with open(pending, encoding="utf-8") as f:
        task = json.load(f)

    raw = args.signal_json.read_text(encoding="utf-8")
    try:
        signal = parse_response(raw)
        validate_signal(signal)
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        print(f"検証エラー: {e}", file=sys.stderr)
        sys.exit(1)

    asof = date.fromisoformat(args.asof)
    path = signal_path(args.signals_dir, asof, args.code)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "code": args.code,
        "name": task["name"],
        "asof": args.asof,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        # evaluate_historical.extract_model が読める形式でモデル名を埋め込む
        "generator": f"historical (run_historical.py, {args.model})",
        "generated_by": "claude-code-agent",
        "price_as_of": task["price_as_of"],
        "signal": signal,
        "raw_response": raw,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"[OK ] {args.asof} {args.code}: {signal['signal']}"
          f" (confidence={signal['confidence']}) -> {path}")


if __name__ == "__main__":
    main()
