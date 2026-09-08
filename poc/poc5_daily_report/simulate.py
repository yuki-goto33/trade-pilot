"""シグナル完全遵従の売買シミュレーション（毎日フル再計算 / stateless replay）。

ルールの正: specs/2026-09-08-firebase-portfolio-sim-design.md
- buy シグナル当日の始値で現金の allocation（既定10%）を投入（端株可）
- イグジット: 期間満了は始値売りが時系列的に先。日中は stop（安値タッチ、
  約定 min(始値, stop)）が target（高値タッチ、約定 max(始値, target)）より優先
- 保有中の同銘柄 buy は無視。手数料・スリッページ・税ゼロ
- 株式分割は株数・基準価格（entry/target/stop）を比率調整
- 価格欠損日は前日終値で評価を持ち越す
"""
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path
import json

import pandas as pd


@dataclass
class Position:
    code: str
    name: str
    entry_date: str
    entry_price: float
    shares: float
    target_price: float = None
    stop_loss: float = None
    holding_period_days: int = 30
    last_close: float = None  # 価格欠損日の評価持ち越し用

    @property
    def expiry(self) -> date_cls:
        d = date_cls.fromisoformat(self.entry_date)
        return d + timedelta(days=self.holding_period_days or 30)


@dataclass
class SimulationResult:
    daily: list = field(default_factory=list)      # [{date, equity, cash, n_positions}]
    trades: list = field(default_factory=list)     # 決済済み取引
    positions: list = field(default_factory=list)  # 現在ポジション（評価付き）
    initial_cash: float = 0.0


def load_signal_history(history_dir: Path) -> dict:
    """signals_history/<date>/<code>.json → {date: [シグナル dict]} に平坦化。

    壊れた JSON・必須キー欠損はスキップして続行する（朝バッチの部分失敗に耐える）。
    """
    history = {}
    for day_dir in sorted(Path(history_dir).iterdir()):
        if not day_dir.is_dir():
            continue
        signals = []
        for f in sorted(day_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                sig = rec["signal"]
                signals.append({
                    "code": rec["code"],
                    "name": rec.get("name", rec["code"]),
                    "signal": sig["signal"],
                    "target_price": sig.get("target_price"),
                    "stop_loss": sig.get("stop_loss"),
                    "holding_period_days": sig.get("holding_period_days") or 30,
                })
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        if signals:
            history[day_dir.name] = signals
    return history


def fetch_ohlc(codes: list, start: str) -> dict:
    """yfinance で複数銘柄の日足 OHLC（未調整）を一括取得する。

    分割検出のため actions（Stock Splits）も含める。無料 API。
    """
    import yfinance as yf
    tickers = [f"{c}.T" if not c.endswith(".T") and not c.startswith("^") else c
               for c in codes]
    data = yf.download(tickers, start=start, auto_adjust=False, actions=True,
                       group_by="ticker", progress=False, threads=True)
    out = {}
    for code, ticker in zip(codes, tickers):
        try:
            df = data[ticker] if len(tickers) > 1 else data
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if not df.empty:
                out[code] = df
        except KeyError:
            continue
    return out


def _split_ratio(row) -> float:
    """その日の分割比率（なければ 1.0）。"""
    try:
        r = float(row.get("Stock Splits", 0) or 0)
        return r if r > 0 else 1.0
    except (TypeError, ValueError):
        return 1.0


def run_simulation(signals_by_date: dict, ohlc_by_code: dict,
                   initial_cash: float = 3_000_000,
                   allocation: float = 0.10) -> SimulationResult:
    """日次リプレイ。取引カレンダーは全銘柄の価格日付の和集合。"""
    all_dates = set()
    for df in ohlc_by_code.values():
        all_dates.update(d.date().isoformat() for d in df.index)
    if not all_dates:
        return SimulationResult(initial_cash=initial_cash)
    start = min(signals_by_date.keys()) if signals_by_date else min(all_dates)
    calendar = sorted(d for d in all_dates if d >= start)

    # 高速参照用: code → {date: row}
    rows_by_code = {
        code: {d.date().isoformat(): row for d, row in df.iterrows()}
        for code, df in ohlc_by_code.items()
    }

    cash = initial_cash
    holdings = {}  # code -> Position
    result = SimulationResult(initial_cash=initial_cash)

    for today in calendar:
        today_date = date_cls.fromisoformat(today)
        today_signals = {s["code"]: s for s in signals_by_date.get(today, [])}

        # --- 1. イグジット判定（保有銘柄ごと・時系列順） ---
        for code in list(holdings.keys()):
            pos = holdings[code]
            row = rows_by_code.get(code, {}).get(today)
            if row is None:
                continue  # 価格欠損日は判定せず評価持ち越し

            # 株式分割: 株数を増やし基準価格を割る（評価額は不変）
            ratio = _split_ratio(row)
            if ratio != 1.0:
                pos.shares *= ratio
                pos.entry_price /= ratio
                if pos.target_price:
                    pos.target_price /= ratio
                if pos.stop_loss:
                    pos.stop_loss /= ratio

            o, h, low = float(row["Open"]), float(row["High"]), float(row["Low"])
            exit_price = exit_reason = None

            if today_date >= pos.expiry:
                # 満了日の始値売りは 9:00 に成立し、日中の stop/target より時系列で先
                exit_price, exit_reason = o, "holding_period"
            elif today_signals.get(code, {}).get("signal") == "sell":
                exit_price, exit_reason = o, "sell_signal"
            elif pos.stop_loss and low <= pos.stop_loss:
                # 同日に target も触れた場合は保守的に stop を優先
                exit_price, exit_reason = min(o, pos.stop_loss), "stop_loss"
            elif pos.target_price and h >= pos.target_price:
                exit_price, exit_reason = max(o, pos.target_price), "target"

            if exit_reason:
                cash += pos.shares * exit_price
                pnl = (exit_price - pos.entry_price) * pos.shares
                result.trades.append({
                    "code": code, "name": pos.name,
                    "entry_date": pos.entry_date,
                    "entry_price": round(pos.entry_price, 4),
                    "exit_date": today, "exit_price": round(exit_price, 4),
                    "shares": round(pos.shares, 4),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round((exit_price / pos.entry_price - 1) * 100, 2),
                    "exit_reason": exit_reason,
                })
                del holdings[code]
            else:
                pos.last_close = float(row["Close"])

        # --- 2. エントリー（buy シグナル・保有中は無視） ---
        for code, sig in today_signals.items():
            if sig["signal"] != "buy" or code in holdings:
                continue
            row = rows_by_code.get(code, {}).get(today)
            if row is None:
                continue  # 当日価格がなければ見送り
            o = float(row["Open"])
            budget = cash * allocation
            if budget <= 0 or o <= 0:
                continue
            shares = budget / o
            cash -= budget
            holdings[code] = Position(
                code=code, name=sig["name"], entry_date=today, entry_price=o,
                shares=shares, target_price=sig.get("target_price"),
                stop_loss=sig.get("stop_loss"),
                holding_period_days=sig.get("holding_period_days") or 30,
                last_close=float(row["Close"]),
            )

        # --- 3. 日次評価 ---
        equity = cash + sum(
            p.shares * (p.last_close or p.entry_price) for p in holdings.values())
        result.daily.append({
            "date": today, "equity": round(equity, 2),
            "cash": round(cash, 2), "n_positions": len(holdings),
        })

    # --- 現在ポジションの評価 ---
    for code, pos in holdings.items():
        last = pos.last_close or pos.entry_price
        result.positions.append({
            "code": code, "name": pos.name,
            "entry_date": pos.entry_date,
            "entry_price": round(pos.entry_price, 4),
            "shares": round(pos.shares, 4),
            "last_price": round(last, 4),
            "value": round(pos.shares * last, 2),
            "unrealized_pnl_pct": round((last / pos.entry_price - 1) * 100, 2),
        })
    result.positions.sort(key=lambda p: p["entry_date"])
    return result
