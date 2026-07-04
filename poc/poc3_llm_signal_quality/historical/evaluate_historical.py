"""ヒストリカル用: 生成済みシグナルの評価（バックテスト + 方向的中率 + TOPIX 比較）。

流れ:
1. data/signals_historical/ を signals.csv (date,code,action,confidence) に変換
   - シグナルは「as-of 日 D の朝に、D-1 引けまでのデータで生成」したもの。
     poc4 の意味論（date 行 = その日の引け時点の判断 → 翌営業日寄り付きで約定）に
     合わせるため、CSV の date には price_as_of（= D の前営業日）を使う。
     これで約定は D の寄り付きになり、look-ahead なしで整合する。
2. prices_history.csv を poc4 のローダー形式（OHLCV DataFrame 群）に変換し、
   poc4 run_signals の SignalStrategy でバックテスト
   （初期資金 1,000 万円/銘柄・手数料 0.1%・100株単元）
3. 同期間の TOPIX(1306.T) リターンと比較
4. 方向的中率: 各 buy/sell シグナルについて holding_period_days 以内に
   target 到達（的中）/ stop 到達（外れ）/ 期限切れ時の含み損益方向 を判定し、
   55% 基準と照合
5. 結果を data/historical_eval_report.md に出力

使い方:
    ../../../.venv/bin/python evaluate_historical.py
    ../../../.venv/bin/python evaluate_historical.py --min-confidence 60
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HIST_DIR = Path(__file__).resolve().parent
POC3_DIR = HIST_DIR.parent
REPO_ROOT = POC3_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data"
POC4_DIR = REPO_ROOT / "poc" / "poc4_backtest_framework"

sys.path.insert(0, str(HIST_DIR))
sys.path.insert(0, str(POC3_DIR))
sys.path.insert(0, str(POC4_DIR))

from historical_context import (  # noqa: E402
    PRICES_CACHE,
    _load_macro_history,
    _load_price_history,
)
from run_golden_cross import CASH, LOT_SIZE, portfolio_summary, run_backtests  # noqa: E402
from run_signals import SignalStrategy, load_signals  # noqa: E402

SIGNALS_HIST_DIR = DATA_DIR / "signals_historical"
SIGNALS_CSV = SIGNALS_HIST_DIR / "signals.csv"
REPORT_PATH = DATA_DIR / "historical_eval_report.md"

JST = timezone(timedelta(hours=9))
HIT_RATE_TARGET_PCT = 55.0

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


# ---------------------------------------------------------------------------
# シグナル読み込み・CSV 変換
# ---------------------------------------------------------------------------

def load_signal_records() -> list:
    """data/signals_historical/<date>/<code>.json を全件読み込む。"""
    records = []
    for path in sorted(SIGNALS_HIST_DIR.glob("????-??-??/*.json")):
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))
    if not records:
        raise SystemExit(f"{SIGNALS_HIST_DIR} にシグナルがありません。"
                         "run_historical.py を先に実行してください。")
    return records


def write_signals_csv(records: list) -> pd.DataFrame:
    """poc4 用の signals.csv を書き出す（date は price_as_of = as-of の前営業日）。"""
    rows = [
        {
            "date": r["price_as_of"],
            "code": r["code"],
            "action": r["signal"]["signal"],
            "confidence": r["signal"]["confidence"],
        }
        for r in records
    ]
    df = pd.DataFrame(rows).sort_values(["date", "code"])
    df.to_csv(SIGNALS_CSV, index=False)
    return df


# ---------------------------------------------------------------------------
# 価格データ（yfinance キャッシュ → poc4 ローダー形式）
# ---------------------------------------------------------------------------

def load_prices_yf(start=None) -> dict:
    """prices_history.csv（異常値除去済み）を {code: OHLCV DataFrame} に変換する。"""
    df = _load_price_history()
    out = {}
    for ticker, g in df.groupby("ticker"):
        code = str(ticker).replace(".T", "")
        ohlcv = (
            g.set_index("Date").sort_index()[_OHLCV_COLS]
            .dropna(subset=["Open", "High", "Low", "Close"])
        )
        if start is not None:
            ohlcv = ohlcv[ohlcv.index >= pd.Timestamp(start)]
        out[code] = ohlcv
    return out


def topix_return_pct(start, end) -> "tuple":
    """同期間の TOPIX 連動 ETF (1306.T) リターン [%] と実際の日付範囲。"""
    df = _load_macro_history()
    s = df[(df["ticker"] == "1306.T")
           & (df["Date"] >= pd.Timestamp(start))
           & (df["Date"] <= pd.Timestamp(end))].sort_values("Date")
    if len(s) < 2:
        return None, None, None
    ret = (s["Close"].iloc[-1] / s["Close"].iloc[0] - 1) * 100
    return round(ret, 2), s["Date"].iloc[0].date(), s["Date"].iloc[-1].date()


# ---------------------------------------------------------------------------
# 方向的中率
# ---------------------------------------------------------------------------

def judge_direction(record: dict, ohlcv: pd.DataFrame) -> dict:
    """buy/sell シグナル 1 件の方向的中を判定する。

    - エントリー: as-of 日（= price_as_of の翌営業日）の寄り付き
    - holding_period_days（暦日）以内に target 到達 → hit / stop 到達 → miss
    - どちらも到達せず期限切れ → 期限時点の含み損益の方向で判定
    - 価格データが期限まで無い場合は最終バーで判定し truncated=True
    """
    sig = record["signal"]
    action = sig["signal"]
    asof = pd.Timestamp(record["asof"])
    target, stop = sig["target_price"], sig["stop_loss"]
    hpd = sig["holding_period_days"]

    window = ohlcv[ohlcv.index >= asof]
    if window.empty:
        return {"judged": False, "why": "as-of 以降の価格データなし"}
    entry = float(window["Open"].iloc[0])
    deadline = asof + pd.Timedelta(days=hpd)
    window = window[window.index <= deadline]
    truncated = ohlcv.index.max() < deadline

    outcome, hit = None, None
    for ts, bar in window.iterrows():
        if action == "buy":
            hit_target = target is not None and bar["High"] >= target
            hit_stop = stop is not None and bar["Low"] <= stop
        else:  # sell（下方向が的中）
            hit_target = target is not None and bar["Low"] <= target
            hit_stop = stop is not None and bar["High"] >= stop
        if hit_target and hit_stop:
            # 同一バーで両方到達 → どちらが先か不明なので保守的に外れ扱い
            outcome, hit = "target_and_stop_same_bar", False
            break
        if hit_stop:
            outcome, hit = "stop_hit", False
            break
        if hit_target:
            outcome, hit = "target_hit", True
            break
    if outcome is None:
        last_close = float(window["Close"].iloc[-1])
        pnl = last_close - entry if action == "buy" else entry - last_close
        outcome, hit = ("expired_truncated" if truncated else "expired"), pnl > 0

    return {
        "judged": True,
        "asof": record["asof"],
        "code": record["code"],
        "action": action,
        "confidence": sig["confidence"],
        "entry": entry,
        "target": target,
        "stop": stop,
        "holding_period_days": hpd,
        "outcome": outcome,
        "hit": hit,
        "truncated": truncated,
    }


def direction_hit_rates(records: list, prices: dict) -> "tuple":
    """buy/sell 全シグナルの方向的中判定と集計を返す。"""
    judged, skipped = [], 0
    for r in records:
        if r["signal"]["signal"] == "hold":
            continue
        ohlcv = prices.get(r["code"])
        if ohlcv is None:
            skipped += 1
            continue
        res = judge_direction(r, ohlcv)
        if res.get("judged"):
            judged.append(res)
        else:
            skipped += 1

    def rate(items):
        return round(100 * sum(1 for x in items if x["hit"]) / len(items), 1) if items else None

    stats = {
        "n_judged": len(judged),
        "n_skipped": skipped,
        "hit_rate_pct": rate(judged),
        "by_action": {
            a: {"n": len(g), "hit_rate_pct": rate(g)}
            for a in ("buy", "sell")
            for g in [[x for x in judged if x["action"] == a]]
            if g
        },
        "outcomes": pd.Series([x["outcome"] for x in judged]).value_counts().to_dict()
        if judged else {},
        "n_truncated": sum(1 for x in judged if x["truncated"]),
    }
    return judged, stats


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

def build_report(records, sig_df, summary, port, topix, hit_stats, args) -> str:
    dist = sig_df["action"].value_counts().to_dict()
    dates = sorted({r["asof"] for r in records})
    codes = sorted({r["code"] for r in records})
    conf = sig_df["confidence"]
    topix_ret, topix_from, topix_to = topix

    lines = []
    lines.append("# ヒストリカルシミュレーション評価レポート")
    lines.append("")
    lines.append(f"- 生成日時: {datetime.now(JST).isoformat(timespec='seconds')}")
    lines.append(f"- シグナル期間 (as-of): {dates[0]} 〜 {dates[-1]}"
                 f"（{len(dates)} 営業日 × {len(codes)} 銘柄 = {len(records)} 件）")
    lines.append(f"- 確信度足切り (--min-confidence): {args.min_confidence}")
    lines.append("- シグナル生成: 各 as-of 日の朝に「前営業日引けまでの"
                 "テクニカル・直前3日ニュース・90日開示・公表済みファンダ・マクロ」のみで生成")
    lines.append("- 約定タイミング: as-of 日の寄り付き（poc4 の翌営業日寄り付き約定に整合）")
    lines.append("")

    lines.append("## 1. シグナル分布")
    lines.append("")
    lines.append("| action | 件数 | 比率 |")
    lines.append("|---|---:|---:|")
    for a in ("buy", "sell", "hold"):
        n = dist.get(a, 0)
        lines.append(f"| {a} | {n} | {100 * n / len(sig_df):.1f}% |")
    lines.append("")
    lines.append(f"confidence: 平均 {conf.mean():.1f} / 中央値 {conf.median():.0f}"
                 f" / 最小 {conf.min()} / 最大 {conf.max()}")
    lines.append("")

    lines.append("## 2. 方向的中率（buy/sell シグナル）")
    lines.append("")
    lines.append("判定: holding_period_days（暦日）以内に target 到達=的中 / "
                 "stop 到達=外れ / 期限切れは含み損益の方向。")
    lines.append("")
    if hit_stats["n_judged"]:
        lines.append(f"- 判定対象: {hit_stats['n_judged']} 件"
                     f"（スキップ {hit_stats['n_skipped']} 件、"
                     f"期限が価格データ末尾を超過 {hit_stats['n_truncated']} 件）")
        hr = hit_stats["hit_rate_pct"]
        verdict = "達成" if hr is not None and hr >= HIT_RATE_TARGET_PCT else "未達"
        lines.append(f"- **方向的中率: {hr}%（基準 {HIT_RATE_TARGET_PCT}% → {verdict}）**")
        for a, st in hit_stats["by_action"].items():
            lines.append(f"  - {a}: {st['hit_rate_pct']}%（{st['n']} 件）")
        lines.append(f"- 内訳: {hit_stats['outcomes']}")
    else:
        lines.append("- buy/sell シグナルが無いため判定対象なし（全 hold）")
    lines.append("")

    lines.append("## 3. シグナル駆動バックテスト")
    lines.append("")
    lines.append(f"条件: 初期資金 {CASH:,} 円/銘柄・手数料 0.1%・単元 {LOT_SIZE} 株・"
                 "現物ロングのみ（sell は手仕舞い）・期末強制決済")
    lines.append("")
    if summary is not None:
        lines.append("| code | bars | trades | 勝率% | 損益(円) | リターン% | B&H% | 最大DD% |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for _, r in summary.iterrows():
            lines.append(
                f"| {r['code']} | {r['bars']} | {r['trades']} | {r['win_rate_pct']} "
                f"| {r['pnl_jpy']:,} | {r['return_pct']} | {r['buy_hold_pct']} "
                f"| {r['max_dd_pct']} |"
            )
        lines.append("")
        lines.append(f"### ポートフォリオ合算（{len(summary)} 銘柄）")
        lines.append("")
        for k, v in port.items():
            lines.append(f"- {k}: {v:,}")
    else:
        lines.append("バックテスト対象銘柄なし")
    lines.append("")

    lines.append("## 4. TOPIX 比較")
    lines.append("")
    if topix_ret is not None:
        port_ret = port["total_return_pct"] if port else None
        lines.append(f"- 評価期間: {topix_from} 〜 {topix_to}")
        lines.append(f"- TOPIX 連動 ETF (1306.T) リターン: {topix_ret}%")
        if port_ret is not None:
            diff = round(port_ret - topix_ret, 2)
            lines.append(f"- ポートフォリオリターン: {port_ret}% → **対 TOPIX {diff:+}pt**")
    else:
        lines.append("- TOPIX データ不足のため比較不可")
    lines.append("")

    lines.append("## 5. 注意事項")
    lines.append("")
    lines.append("- ファンダのバリュエーションは as-of 株価 × 直近公表 EPS/現在 BPS 逆算の近似値。")
    lines.append("- 業績の公表判定は yanoshin 決算短信日付（無ければ期末+45日）による推定。")
    lines.append("- 過去時点の金利・開示 PDF 要約はコンテキストに含まれない（入手不可）。")
    lines.append("- バックテストはポジション未決済のまま評価期間末に強制決済するため、")
    lines.append("  holding_period_days とは独立に sell シグナルが出るまで保有し続ける。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="この確信度(0-100)未満のシグナルを無視")
    args = parser.parse_args()

    records = load_signal_records()
    sig_df = write_signals_csv(records)
    print(f"signals.csv: {len(sig_df)} 行 -> {SIGNALS_CSV}")
    print(sig_df["action"].value_counts().to_string())

    # バックテスト（価格は最初のシグナル date から末尾まで）
    signal_map = load_signals(SIGNALS_CSV)
    start = sig_df["date"].min()
    prices = load_prices_yf(start=start)
    target = {c: prices[c] for c in prices if c in signal_map}

    summary, port = None, None
    if target:
        rows, curves = [], {}
        for code in sorted(target):
            s, e = run_backtests(
                SignalStrategy,
                prices={code: target[code]},
                signals=signal_map[code],
                min_confidence=args.min_confidence,
            )
            rows.append(s)
            curves.update(e)
        summary = pd.concat(rows, ignore_index=True)
        port = portfolio_summary(summary, curves)
        print("\n=== バックテスト銘柄別サマリー ===")
        print(summary.to_string(index=False))
        print("\n=== ポートフォリオ合算 ===")
        for k, v in port.items():
            print(f"{k}: {v:,}")

    # TOPIX 比較（バックテストと同じ日付範囲）
    price_end = max(df.index.max() for df in target.values()) if target else None
    topix = topix_return_pct(start, price_end) if price_end is not None else (None,) * 3

    # 方向的中率
    judged, hit_stats = direction_hit_rates(records, prices)
    print(f"\n=== 方向的中率 === {hit_stats['hit_rate_pct']}%"
          f" (n={hit_stats['n_judged']}, 基準 {HIT_RATE_TARGET_PCT}%)")
    print(f"内訳: {hit_stats['outcomes']}")

    report = build_report(records, sig_df, summary, port, topix, hit_stats, args)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\nレポート出力: {REPORT_PATH}")

    detail_path = SIGNALS_HIST_DIR / "direction_judgements.json"
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(judged, f, ensure_ascii=False, indent=2)
    print(f"的中判定の明細: {detail_path}")


if __name__ == "__main__":
    main()
