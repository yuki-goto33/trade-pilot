"""simulate.py の単体テスト（合成価格・合成シグナル）。

ルールの正は specs/2026-09-08-firebase-portfolio-sim-design.md。
"""
import json

import pandas as pd
import pytest

from simulate import load_signal_history, run_simulation


def make_ohlc(rows: dict) -> pd.DataFrame:
    """{date: (open, high, low, close)} → OHLC DataFrame（DatetimeIndex）。"""
    idx = pd.to_datetime(list(rows.keys()))
    data = {
        "Open": [v[0] for v in rows.values()],
        "High": [v[1] for v in rows.values()],
        "Low": [v[2] for v in rows.values()],
        "Close": [v[3] for v in rows.values()],
    }
    return pd.DataFrame(data, index=idx)


def buy_signal(code="7203", name="トヨタ", target=None, stop=None, days=30):
    return {
        "code": code,
        "name": name,
        "signal": "buy",
        "target_price": target,
        "stop_loss": stop,
        "holding_period_days": days,
    }


def test_entry_at_open_with_10pct_allocation():
    """buy 当日の始値で現金の10%を投入（端株可）。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (1005, 1015, 995, 1010),
    })}
    signals = {"2026-07-01": [buy_signal()]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000, allocation=0.10)
    assert len(result.positions) == 1
    pos = result.positions[0]
    assert pos["entry_price"] == 1000
    assert pos["shares"] == pytest.approx(300_000 / 1000)  # 現金300万の10%
    # 最終日の評価: 現金270万 + 300株×終値1010
    assert result.daily[-1]["equity"] == pytest.approx(2_700_000 + 300 * 1010)


def test_stop_loss_exit_with_gap_down():
    """安値が stop 以下 → 約定は min(始値, stop)。ギャップダウンなら始値。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (900, 920, 880, 910),  # 始値900 < stop950 のギャップダウン
    })}
    signals = {"2026-07-01": [buy_signal(stop=950)]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert tr["exit_reason"] == "stop_loss"
    assert tr["exit_price"] == 900  # min(900, 950)
    assert tr["exit_date"] == "2026-07-02"


def test_target_exit_with_gap_up():
    """高値が target 以上 → 約定は max(始値, target)。ギャップアップなら始値。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (1100, 1120, 1090, 1110),  # 始値1100 > target1050
    })}
    signals = {"2026-07-01": [buy_signal(target=1050)]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    tr = result.trades[0]
    assert tr["exit_reason"] == "target"
    assert tr["exit_price"] == 1100  # max(1100, 1050)


def test_same_day_both_touch_stop_wins():
    """同日に stop と target 両方タッチ → 保守的に stop 優先。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (1000, 1100, 900, 1000),  # 高値1100≥target, 安値900≤stop
    })}
    signals = {"2026-07-01": [buy_signal(target=1050, stop=950)]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    tr = result.trades[0]
    assert tr["exit_reason"] == "stop_loss"
    assert tr["exit_price"] == 950  # min(1000, 950)


def test_holding_period_expiry_sells_at_open():
    """holding_period_days（暦日）経過後、最初の営業日の始値で売却。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (1005, 1015, 995, 1010),
        "2026-07-06": (1020, 1030, 1010, 1025),  # 7/1+3日=7/4経過後の最初の営業日
    })}
    signals = {"2026-07-01": [buy_signal(days=3)]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    tr = result.trades[0]
    assert tr["exit_reason"] == "holding_period"
    assert tr["exit_date"] == "2026-07-06"
    assert tr["exit_price"] == 1020


def test_expiry_at_open_precedes_intraday_stop():
    """期間満了日は始値売りが時系列的に先 → 同日の日中 stop より優先。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-06": (1000, 1010, 900, 950),  # 安値900は stop 950 以下だが満了日
    })}
    signals = {"2026-07-01": [buy_signal(stop=950, days=3)]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    tr = result.trades[0]
    assert tr["exit_reason"] == "holding_period"
    assert tr["exit_price"] == 1000


def test_rebuy_while_holding_is_ignored():
    """保有中の同銘柄 buy 再シグナルは無視（追加買いなし）。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (1005, 1015, 995, 1010),
    })}
    signals = {
        "2026-07-01": [buy_signal()],
        "2026-07-02": [buy_signal()],
    }
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    assert len(result.positions) == 1
    assert result.positions[0]["entry_date"] == "2026-07-01"


def test_sell_signal_exits_at_open():
    """sell シグナル → 当日始値で売却。"""
    ohlc = {"7203": make_ohlc({
        "2026-07-01": (1000, 1010, 990, 1005),
        "2026-07-02": (1008, 1015, 995, 1010),
    })}
    signals = {
        "2026-07-01": [buy_signal()],
        "2026-07-02": [dict(buy_signal(), signal="sell")],
    }
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    tr = result.trades[0]
    assert tr["exit_reason"] == "sell_signal"
    assert tr["exit_price"] == 1008


def test_missing_price_day_carries_last_value():
    """価格欠損日は前日評価を持ち越す（equity が計算できる）。"""
    ohlc = {
        "7203": make_ohlc({
            "2026-07-01": (1000, 1010, 990, 1005),
            "2026-07-02": (1005, 1015, 995, 1010),
        }),
        "6758": make_ohlc({
            "2026-07-01": (500, 510, 490, 505),
            "2026-07-02": (505, 515, 495, 510),
            "2026-07-03": (510, 520, 500, 515),  # 7203 は 7/3 欠損
        }),
    }
    signals = {"2026-07-01": [buy_signal(), buy_signal(code="6758", name="ソニーG")]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    # 7/3: 7203 は前日終値1010で評価維持
    last = result.daily[-1]
    assert last["date"] == "2026-07-03"
    shares_7203 = 300_000 / 1000
    shares_6758 = 270_000 / 500
    expected = (3_000_000 - 300_000 - 270_000
                + shares_7203 * 1010 + shares_6758 * 515)
    assert last["equity"] == pytest.approx(expected)


def test_hold_signal_no_trade():
    """hold シグナルでは取引しない。"""
    ohlc = {"7203": make_ohlc({"2026-07-01": (1000, 1010, 990, 1005)})}
    signals = {"2026-07-01": [dict(buy_signal(), signal="hold")]}
    result = run_simulation(signals, ohlc, initial_cash=3_000_000)
    assert result.trades == []
    assert result.positions == []
    assert result.daily[-1]["equity"] == 3_000_000


def test_load_signal_history(tmp_path):
    """signals_history/<date>/<code>.json を日付→シグナル一覧に平坦化。壊れた JSON はスキップ。"""
    d = tmp_path / "2026-07-01"
    d.mkdir()
    (d / "7203.json").write_text(json.dumps({
        "code": "7203", "name": "トヨタ自動車",
        "signal": {"signal": "buy", "confidence": 80, "target_price": 3300.0,
                   "stop_loss": 3000.0, "holding_period_days": 30},
    }), encoding="utf-8")
    (d / "9999.json").write_text("{broken", encoding="utf-8")
    history = load_signal_history(tmp_path)
    assert list(history.keys()) == ["2026-07-01"]
    assert len(history["2026-07-01"]) == 1
    sig = history["2026-07-01"][0]
    assert sig["code"] == "7203"
    assert sig["signal"] == "buy"
    assert sig["target_price"] == 3300.0
    assert sig["holding_period_days"] == 30
