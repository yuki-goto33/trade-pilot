"""テスト用ダミーシグナル生成: ゴールデンクロスから signals.csv を機械生成する。

将来 LLM が出力するシグナルと同じ形式
    date, code, action(buy/sell/hold), confidence(0.0-1.0)
の CSV を、SMA25/75 のクロスから機械的に生成する。

- ゴールデンクロス日 -> buy
- デッドクロス日     -> sell
- それ以外は 10 営業日ごとに hold 行を出力（hold の無視と confidence 列の疎通確認用）
- confidence は直近 5 日リターンの大きさから決定論的に算出（0.5〜0.99）
  （クロス時点では SMA 乖離がほぼ 0 のため、乖離でなくモメンタムを使うことで
   confidence にばらつきを持たせ、--min-confidence の足切り動作を確認できるようにする）

生成される buy/sell は run_golden_cross.py の売買タイミングと完全に一致するため、
シグナル駆動 runner の結果がベースラインと一致するかで正しさを検証できる。

実行:
    .venv/bin/python poc/poc4_backtest_framework/make_dummy_signals.py
"""
from pathlib import Path

import pandas as pd

from data_loader import load_prices

OUT = Path(__file__).resolve().parent / "signals.csv"
N_FAST, N_SLOW = 25, 75
HOLD_EVERY = 10  # hold 行を出す間隔（営業日）


def generate_signals(prices=None):
    if prices is None:
        prices = load_prices()
    rows = []
    for code, ohlcv in sorted(prices.items()):
        close = ohlcv["Close"]
        fast = close.rolling(N_FAST).mean()
        slow = close.rolling(N_SLOW).mean()
        # backtesting.lib.crossover と同一の判定:
        # 前バーで strict に下 (上) にあり、当バーで strict に上 (下) に抜けた
        valid = slow.notna() & slow.shift(1).notna()
        cross_up = valid & (fast.shift(1) < slow.shift(1)) & (fast > slow)
        cross_dn = valid & (fast.shift(1) > slow.shift(1)) & (fast < slow)

        for i, date in enumerate(close.index):
            if cross_up.loc[date]:
                action = "buy"
            elif cross_dn.loc[date]:
                action = "sell"
            elif i % HOLD_EVERY == 0:
                action = "hold"
            else:
                continue
            # 直近 5 日モメンタムの大きさベースの決定論的 confidence（hold は低め）
            mom = close.pct_change(5).loc[date]
            conf = 0.5 if pd.isna(mom) else min(0.99, 0.5 + abs(mom) * 8)
            if action == "hold":
                conf = round(min(conf, 0.6), 3)
            rows.append(
                {
                    "date": date.date().isoformat(),
                    "code": code,
                    "action": action,
                    "confidence": round(conf, 3),
                }
            )
    return pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True)


def main():
    signals = generate_signals()
    signals.to_csv(OUT, index=False)
    print(f"saved: {OUT} ({len(signals)} rows)")
    print(signals["action"].value_counts().to_string())
    print(signals.head(8).to_string(index=False))


if __name__ == "__main__":
    main()
