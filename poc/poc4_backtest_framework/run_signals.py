"""シグナル駆動バックテスト runner。

外部生成されたシグナル CSV（列: date, code, action[buy/sell/hold], confidence）を
読み込み、Backtesting.py でバックテストする。将来 LLM が生成するシグナルの
評価基盤となるインターフェース。

シグナルの意味論:
- date 行のシグナルは「その日の引け時点で確定した判断」とみなし、
  翌営業日の寄り付きで約定する（デイリーレポート運用と同じタイミング）
- buy:  ノーポジションなら成行買い（単元株の倍数で買えるだけ）
- sell: ポジションがあれば全量成行売り（現物想定・ロングのみ、ショートはしない）
- hold: 何もしない
- confidence が --min-confidence 未満のシグナルは無視する（確信度足切り）

実行:
    .venv/bin/python poc/poc4_backtest_framework/run_signals.py signals.csv
    .venv/bin/python poc/poc4_backtest_framework/run_signals.py signals.csv --min-confidence 0.7
"""
import argparse
from pathlib import Path

import pandas as pd
from backtesting import Strategy

from data_loader import load_prices
from run_golden_cross import CASH, LOT_SIZE, lot_size, portfolio_summary, run_backtests

RESULTS_DIR = Path(__file__).resolve().parent / "results"
VALID_ACTIONS = {"buy", "sell", "hold"}


def load_signals(csv_path):
    """signals.csv を検証つきで読み込み、{code: {date: (action, confidence)}} を返す。"""
    df = pd.read_csv(csv_path, dtype={"code": str}, parse_dates=["date"])
    missing = {"date", "code", "action", "confidence"} - set(df.columns)
    if missing:
        raise ValueError(f"signals.csv に必要な列がありません: {sorted(missing)}")
    bad = set(df["action"].unique()) - VALID_ACTIONS
    if bad:
        raise ValueError(f"action は {sorted(VALID_ACTIONS)} のいずれか。不正値: {sorted(bad)}")
    dup = df.duplicated(subset=["date", "code"])
    if dup.any():
        raise ValueError(f"date×code の重複が {dup.sum()} 行あります")
    signal_map = {}
    for (code, date), row in df.set_index(["code", "date"]).iterrows():
        signal_map.setdefault(code, {})[date.date()] = (
            row["action"],
            float(row["confidence"]),
        )
    return signal_map


class SignalStrategy(Strategy):
    """外部シグナル列に従って売買する戦略（現物ロングのみ）。"""

    signals = None          # {date.date(): (action, confidence)} を bt.run() で注入
    min_confidence = 0.0

    def init(self):
        if self.signals is None:
            raise ValueError("signals パラメータが未指定です")

    def next(self):
        sig = self.signals.get(self.data.index[-1].date())
        if sig is None:
            return
        action, confidence = sig
        if confidence < self.min_confidence:
            return
        if action == "buy" and not self.position:
            size = lot_size(self.equity, self.data.Close[-1])
            if size > 0:
                self.buy(size=size)
        elif action == "sell" and self.position:
            self.position.close()
        # hold: 何もしない


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signals_csv", help="シグナル CSV (date,code,action,confidence)")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="この確信度未満のシグナルを無視 (default: 0.0)")
    args = parser.parse_args()

    signal_map = load_signals(args.signals_csv)
    prices = load_prices()

    unknown = set(signal_map) - set(prices)
    if unknown:
        print(f"[warn] 価格データのない銘柄コードのシグナルをスキップ: {sorted(unknown)}")

    target = {c: prices[c] for c in prices if c in signal_map}
    summary_rows = []
    equity_curves = {}
    for code in sorted(target):
        s, e = run_backtests(
            SignalStrategy,
            prices={code: target[code]},
            signals=signal_map[code],
            min_confidence=args.min_confidence,
        )
        summary_rows.append(s)
        equity_curves.update(e)
    summary = pd.concat(summary_rows, ignore_index=True)
    port = portfolio_summary(summary, equity_curves)

    print("=== シグナル駆動バックテスト 銘柄別サマリー ===")
    print(f"signals: {args.signals_csv}  min_confidence: {args.min_confidence}")
    print(f"条件: 初期資金 {CASH:,} 円/銘柄, 手数料 0.1%, 単元 {LOT_SIZE} 株\n")
    print(summary.to_string(index=False))
    print(f"\n=== ポートフォリオ合算 ({len(summary)}銘柄) ===")
    for k, v in port.items():
        print(f"{k}: {v:,}")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / "signal_backtest_results.csv"
    summary.to_csv(out, index=False)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
