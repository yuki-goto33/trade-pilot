"""ヒストリカル用: 過去営業日 × 銘柄で LLM シグナルを生成する。

generate_signal.py の render_prompts / parse_response / validate_signal を再利用し、
コンテキストのみ historical_context.build_context_asof で as-of 時点に差し替える。

- 保存先: data/signals_historical_v2/<YYYY-MM-DD>/<code>.json
  （--signals-dir で変更可。v1 の生成物は data/signals_historical/ に保持）
- 生成済み（ファイル存在）はスキップ → resume 対応
- --max-calls N で 1 回の実行の LLM 呼び出し数を制限（チャンク実行用）
- 進捗を stdout と <signals-dir>/progress.json に出力

使い方:
    ../../../.venv/bin/python run_historical.py --start 2026-04-01 --end 2026-04-30 --max-calls 40
    ../../../.venv/bin/python run_historical.py --start 2026-04-01 --end 2026-04-10 --codes 7203 6758
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
sys.path.insert(0, str(POC3_DIR))
sys.path.insert(0, str(HIST_DIR))

from context_builder import ContextBuildError  # noqa: E402
from generate_signal import parse_response, render_prompts, validate_signal  # noqa: E402
from historical_context import (  # noqa: E402
    DATA_DIR,
    UNIVERSE,
    build_context_asof,
    trading_dates,
)
from llm_client import (  # noqa: E402
    DEFAULT_ROTATION_MODELS as DEFAULT_MODELS,
    RotatingGeminiClient,
)

# v2 の既定保存先（v1: signals_historical は保持したまま分離）
DEFAULT_SIGNALS_DIR = DATA_DIR / "signals_historical_v2"

JST = timezone(timedelta(hours=9))


def signal_path(signals_dir: Path, asof, code: str) -> Path:
    return signals_dir / asof.isoformat() / f"{code}.json"


def save_signal_asof(signals_dir: Path, asof, code: str, name: str, context: dict,
                     signal: dict, raw: str, model: str = None,
                     expert_views: dict = None) -> Path:
    path = signal_path(signals_dir, asof, code)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "code": code,
        "name": name,
        "asof": asof.isoformat(),
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "generator": f"historical (run_historical.py, {model or 'gemini'})",
        "price_as_of": context["price_technical"]["as_of"],
        "signal": signal,
        "raw_response": raw,
    }
    if expert_views:
        record["expert_views"] = expert_views
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def write_progress(signals_dir: Path, stats: dict) -> None:
    signals_dir.mkdir(parents=True, exist_ok=True)
    stats = dict(stats)
    stats["updated_at"] = datetime.now(JST).isoformat(timespec="seconds")
    with open(signals_dir / "progress.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="終了日 YYYY-MM-DD（含む）")
    parser.add_argument("--codes", nargs="*", default=None,
                        help="対象銘柄コード（省略時は universe 全銘柄）")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="この実行での LLM 呼び出し数上限（チャンク実行用）")
    parser.add_argument("--models", nargs="*", default=None,
                        help=f"ローテーションする Gemini モデル（既定: {DEFAULT_MODELS}）")
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR,
                        help=f"シグナル保存先ディレクトリ（既定: {DEFAULT_SIGNALS_DIR}）")
    parser.add_argument("--mode", choices=["single", "experts"], default="single",
                        help="single=一括判定（既定、v2互換） / experts=2専門家+統合"
                             "（銘柄あたり LLM 3回。v3 用に別 signals-dir を推奨）")
    args = parser.parse_args()
    signals_dir = args.signals_dir

    codes = args.codes or [s["code"] for s in UNIVERSE]
    unknown = [c for c in codes if c not in {s["code"] for s in UNIVERSE}]
    if unknown:
        parser.error(f"universe に存在しないコード: {unknown}")

    # 対象タスク一覧（営業日 × 銘柄）。営業日は価格キャッシュの実データから決める。
    tasks = []
    for code in codes:
        for d in trading_dates(code, args.start, args.end):
            tasks.append((d, code))
    tasks.sort()

    done_before = sum(1 for d, c in tasks if signal_path(signals_dir, d, c).exists())
    todo = [(d, c) for d, c in tasks if not signal_path(signals_dir, d, c).exists()]
    print(f"対象: {len(tasks)} 件（{args.start}〜{args.end} × {len(codes)} 銘柄）"
          f" / 生成済み {done_before} / 残り {len(todo)}"
          f" / 保存先 {signals_dir}"
          + (f" / 今回上限 {args.max_calls}" if args.max_calls else ""))

    client = RotatingGeminiClient(args.models)
    ok, ng, calls = 0, 0, 0
    started = time.monotonic()

    for asof, code in todo:
        if args.max_calls is not None and calls >= args.max_calls:
            print(f"--max-calls {args.max_calls} に到達したため終了します。")
            break
        try:
            context = build_context_asof(code, asof)
            expert_views = None
            if args.mode == "experts":
                from experts import run_expert_pipeline
                signal, views, raws = run_expert_pipeline(
                    client, context, parse_response, validate_signal,
                    log=lambda m: print(m, file=sys.stderr))
                raw = raws["synthesis"]
                expert_views = views
                calls += 3
            else:
                system, user = render_prompts(context)
                # パース・スキーマ違反は LLM 出力の揺らぎなので 1 回だけ再サンプルする
                for retry in range(2):
                    calls += 1
                    raw = client.complete(system, user)
                    try:
                        signal = parse_response(raw)
                        validate_signal(signal)
                        break
                    except (json.JSONDecodeError, jsonschema.ValidationError):
                        if retry == 1:
                            raise
                        print(f"    [resample] {asof} {code}: スキーマ違反のため再生成",
                              file=sys.stderr)
            save_signal_asof(signals_dir, asof, code, context["meta"]["name"],
                             context, signal, raw, model=client.last_model,
                             expert_views=expert_views)
            ok += 1
            elapsed = time.monotonic() - started
            print(f"[OK ] {asof} {code}: {signal['signal']}"
                  f" (confidence={signal['confidence']}, {client.last_model})"
                  f"  [{ok + ng}/{len(todo)} calls={calls} {elapsed:.0f}s]")
        except (ContextBuildError, json.JSONDecodeError,
                jsonschema.ValidationError, RuntimeError) as e:
            ng += 1
            print(f"[NG ] {asof} {code}: {type(e).__name__}: {e}", file=sys.stderr)

        write_progress(signals_dir, {
            "range": [args.start, args.end],
            "codes": codes,
            "total_tasks": len(tasks),
            "done_total": done_before + ok,
            "remaining": len(tasks) - done_before - ok,
            "this_run": {"ok": ok, "ng": ng, "llm_calls": calls,
                         "elapsed_sec": round(time.monotonic() - started)},
            "last": f"{asof} {code}",
        })

    elapsed = time.monotonic() - started
    remaining = len(tasks) - done_before - ok
    print(f"\n完了: OK {ok} / NG {ng} / LLM 呼び出し {calls} 回 / 所要 {elapsed:.0f}s")
    print(f"全体進捗: {done_before + ok}/{len(tasks)}（残り {remaining}）")
    sys.exit(1 if ng and not ok else 0)


if __name__ == "__main__":
    main()
