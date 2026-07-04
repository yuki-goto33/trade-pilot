"""ヒストリカル用: 過去営業日 × 銘柄で LLM シグナルを生成する。

generate_signal.py の render_prompts / parse_response / validate_signal を再利用し、
コンテキストのみ historical_context.build_context_asof で as-of 時点に差し替える。

- 保存先: data/signals_historical/<YYYY-MM-DD>/<code>.json
- 生成済み（ファイル存在）はスキップ → resume 対応
- --max-calls N で 1 回の実行の LLM 呼び出し数を制限（チャンク実行用）
- 進捗を stdout と data/signals_historical/progress.json に出力

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
from llm_client import GeminiClient, GeminiRateLimitError  # noqa: E402

SIGNALS_HIST_DIR = DATA_DIR / "signals_historical"
PROGRESS_PATH = SIGNALS_HIST_DIR / "progress.json"

JST = timezone(timedelta(hours=9))

# Gemini 無料枠はモデルごとに小さなトークンバケット型クォータのため、
# 既定で複数モデルをローテーションする（クォータ枯渇時に次モデルへ切替）。
DEFAULT_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
]


class RotatingGeminiClient:
    """モデルごとの 429 クォータをまたいでローテーションする Gemini クライアント。

    - 各モデルの GeminiClient は 429 を即時 GeminiRateLimitError で返す設定にし、
      クールダウン（API 提示の retryDelay）を記録して次モデルへ切り替える
    - その他の RuntimeError（503 リトライ上限等）も短いクールダウンで次モデルへ
    - 全モデルがクールダウン中なら最短の解除時刻まで待つ
    - 成功したモデル名を last_model に保持する（シグナル記録用）
    """

    TOTAL_WAIT_CAP_SEC = 2700.0   # 1コンテキストあたりの累計待機上限
    ERROR_COOLDOWN_SEC = 90.0     # 429 以外のエラー時のクールダウン

    def __init__(self, models=None):
        self.models = models or DEFAULT_MODELS
        self.clients = {m: GeminiClient(model=m, rate_limit_max_wait_sec=0.0)
                        for m in self.models}
        self.cooldown_until = {m: 0.0 for m in self.models}
        self.last_model = None

    def complete(self, system: str, user: str) -> str:
        waited = 0.0
        last_err = None
        while True:
            now = time.monotonic()
            available = [m for m in self.models if self.cooldown_until[m] <= now]
            if not available:
                wait = min(self.cooldown_until.values()) - now + 1.0
                if waited + wait > self.TOTAL_WAIT_CAP_SEC:
                    raise RuntimeError(
                        f"全モデルがクールダウン中で待機上限超過: {last_err}")
                time.sleep(wait)
                waited += wait
                continue
            model = available[0]
            try:
                raw = self.clients[model].complete(system, user)
                self.last_model = model
                return raw
            except GeminiRateLimitError as e:
                self.cooldown_until[model] = (
                    time.monotonic() + max(e.retry_delay_sec, 30.0))
                last_err = str(e)[:150]
                print(f"    [rotate] {model}: quota (cooldown "
                      f"{max(e.retry_delay_sec, 30.0):.0f}s)", file=sys.stderr)
            except RuntimeError as e:
                self.cooldown_until[model] = (
                    time.monotonic() + self.ERROR_COOLDOWN_SEC)
                last_err = str(e)[:150]
                print(f"    [rotate] {model}: {last_err}", file=sys.stderr)


def signal_path(asof, code: str) -> Path:
    return SIGNALS_HIST_DIR / asof.isoformat() / f"{code}.json"


def save_signal_asof(asof, code: str, name: str, context: dict,
                     signal: dict, raw: str, model: str = None) -> Path:
    path = signal_path(asof, code)
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def write_progress(stats: dict) -> None:
    SIGNALS_HIST_DIR.mkdir(parents=True, exist_ok=True)
    stats = dict(stats)
    stats["updated_at"] = datetime.now(JST).isoformat(timespec="seconds")
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
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
    args = parser.parse_args()

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

    done_before = sum(1 for d, c in tasks if signal_path(d, c).exists())
    todo = [(d, c) for d, c in tasks if not signal_path(d, c).exists()]
    print(f"対象: {len(tasks)} 件（{args.start}〜{args.end} × {len(codes)} 銘柄）"
          f" / 生成済み {done_before} / 残り {len(todo)}"
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
            save_signal_asof(asof, code, context["meta"]["name"], context,
                             signal, raw, model=client.last_model)
            ok += 1
            elapsed = time.monotonic() - started
            print(f"[OK ] {asof} {code}: {signal['signal']}"
                  f" (confidence={signal['confidence']}, {client.last_model})"
                  f"  [{ok + ng}/{len(todo)} calls={calls} {elapsed:.0f}s]")
        except (ContextBuildError, json.JSONDecodeError,
                jsonschema.ValidationError, RuntimeError) as e:
            ng += 1
            print(f"[NG ] {asof} {code}: {type(e).__name__}: {e}", file=sys.stderr)

        write_progress({
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
