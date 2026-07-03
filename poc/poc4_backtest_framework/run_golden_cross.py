"""ベースライン検証: ゴールデンクロス戦略 (SMA25/75) のバックテスト。

J-Quants 2年日足（11銘柄）に対し Backtesting.py でゴールデンクロス戦略を実行し、
銘柄ごとの勝率・累積損益・最大ドローダウンを出力する。

条件:
- 手数料: 約定代金の 0.1% (COMMISSION)
- 単元株: 100 株単位でのみ発注 (LOT_SIZE)
- 初期資金: 銘柄あたり 1,000 万円
  (68610=キーエンスは株価 5〜7.7 万円のため 1 単元 500〜770 万円。
   500 万円では 1 単元も買えず取引ゼロになることを確認済み → 1,000 万円に設定)
- 約定タイミング: シグナル発生バーの引け後に発注 → 翌営業日の寄り付きで約定
  (Backtesting.py のデフォルト。trade_on_close=False)

実行:
    .venv/bin/python poc/poc4_backtest_framework/run_golden_cross.py
"""
from pathlib import Path

import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

from data_loader import load_prices

CASH = 10_000_000       # 銘柄あたり初期資金（円）
COMMISSION = 0.001      # 手数料 0.1%（約定代金比）
SPREAD = 0.0            # スリッページ相当（bid-ask spread として設定可能）
LOT_SIZE = 100          # 単元株数
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def lot_size(equity, price, lot=LOT_SIZE, safety=0.98):
    """買付可能な単元株数（lot の倍数）を返す。

    翌日寄り付き約定のためのギャップ + 手数料ぶんの余裕として safety を掛ける。
    1 単元も買えない場合は 0。
    """
    units = int(equity * safety / (price * lot))
    return units * lot


class GoldenCross(Strategy):
    """SMA25/75 ゴールデンクロスで買い、デッドクロスで手仕舞い（現物ロングのみ）。"""

    n_fast = 25
    n_slow = 75

    def init(self):
        sma = lambda arr, n: pd.Series(arr).rolling(n).mean()
        self.sma_fast = self.I(sma, self.data.Close, self.n_fast)
        self.sma_slow = self.I(sma, self.data.Close, self.n_slow)

    def next(self):
        if crossover(self.sma_fast, self.sma_slow) and not self.position:
            size = lot_size(self.equity, self.data.Close[-1])
            if size > 0:
                self.buy(size=size)
        elif crossover(self.sma_slow, self.sma_fast) and self.position:
            self.position.close()


def run_backtests(strategy_cls=GoldenCross, prices=None, **strategy_params):
    """全銘柄に対しバックテストを実行し、(サマリー DataFrame, equity 曲線 dict) を返す。"""
    if prices is None:
        prices = load_prices()
    rows = []
    equity_curves = {}
    for code, ohlcv in sorted(prices.items()):
        bt = Backtest(
            ohlcv,
            strategy_cls,
            cash=CASH,
            commission=COMMISSION,
            spread=SPREAD,
            trade_on_close=False,   # 翌営業日寄り付きで約定
            exclusive_orders=False,
            finalize_trades=True,   # 期末に残ポジションを強制決済して統計に含める
        )
        stats = bt.run(**strategy_params)
        equity_curves[code] = stats["_equity_curve"]["Equity"]
        rows.append(
            {
                "code": code,
                "bars": len(ohlcv),
                "trades": stats["# Trades"],
                "win_rate_pct": round(stats["Win Rate [%]"], 1),
                "pnl_jpy": round(stats["Equity Final [$]"] - CASH),
                "return_pct": round(stats["Return [%]"], 2),
                "buy_hold_pct": round(stats["Buy & Hold Return [%]"], 2),
                "max_dd_pct": round(stats["Max. Drawdown [%]"], 2),
            }
        )
    return pd.DataFrame(rows), equity_curves


def portfolio_summary(summary, equity_curves):
    """銘柄別 equity 曲線を合算したポートフォリオ全体の損益と最大DDを返す。"""
    combined = pd.concat(equity_curves.values(), axis=1).ffill().sum(axis=1)
    peak = combined.cummax()
    max_dd = ((combined - peak) / peak).min() * 100
    initial = CASH * len(equity_curves)
    return {
        "total_pnl_jpy": round(combined.iloc[-1] - initial),
        "total_return_pct": round((combined.iloc[-1] / initial - 1) * 100, 2),
        "portfolio_max_dd_pct": round(max_dd, 2),
    }


def main():
    summary, equity_curves = run_backtests()
    port = portfolio_summary(summary, equity_curves)

    print("=== ゴールデンクロス戦略 (SMA25/75) 銘柄別サマリー ===")
    print(f"条件: 初期資金 {CASH:,} 円/銘柄, 手数料 {COMMISSION:.1%}, 単元 {LOT_SIZE} 株\n")
    print(summary.to_string(index=False))
    print("\n=== ポートフォリオ合算 (11銘柄) ===")
    for k, v in port.items():
        print(f"{k}: {v:,}")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / "golden_cross_results.csv"
    summary.to_csv(out, index=False)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
