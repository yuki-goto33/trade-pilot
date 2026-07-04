"""ヒストリカルシミュレーション結果（方向的中率 43.1%, n=137）の敗因分析。

evaluate_historical.py の判定ロジック（judge_direction）をそのまま再利用し、
buy/sell シグナルの的中を切り口別（銘柄・月・confidence・生成モデル・保有期間）に分解する。
さらに buy 失敗の型分類（SMA25 位置・直後の TOPIX 方向・リスクリワード比・
fundamentals 引用の有無）、sell 失敗の内訳、hold の機会損失、truncated 除外時の
的中率を集計し、data/failure_analysis.md に出力する。

LLM API は一切呼ばない（統計・データ分析のみ）。

使い方:
    ../../../.venv/bin/python analyze_failures.py
    ../../../.venv/bin/python analyze_failures.py \
        --signals-dir ../../../data/signals_historical_v2
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HIST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HIST_DIR))

from evaluate_historical import (  # noqa: E402
    DATA_DIR,
    DEFAULT_SIGNALS_DIR,
    extract_model,
    judge_direction,
    load_prices_yf,
    load_signal_records,
)
from historical_context import _load_macro_history  # noqa: E402

JST = timezone(timedelta(hours=9))

TOPIX_TICKER = "1306.T"
FUND_PATTERN = re.compile(r"fundamentals|ファンダ")


def default_report_path(signals_dir: Path) -> Path:
    """signals_historical → failure_analysis.md /
    signals_historical_v2 → failure_analysis_v2.md"""
    suffix = Path(signals_dir).name.replace("signals_historical", "")
    return DATA_DIR / f"failure_analysis{suffix}.md"


# ---------------------------------------------------------------------------
# 判定明細の構築（evaluate_historical の判定ロジックを再利用し属性を付加）
# ---------------------------------------------------------------------------


def sma25_position(ohlcv: pd.DataFrame, price_as_of: pd.Timestamp):
    """price_as_of 時点（シグナル生成時に見えていた最終バー）の終値 vs 25日SMA。

    Returns: ("above"|"below", close, sma25) / データ不足時は (None, None, None)
    """
    hist = ohlcv[ohlcv.index <= price_as_of]
    if len(hist) < 25:
        return None, None, None
    close = float(hist["Close"].iloc[-1])
    sma25 = float(hist["Close"].rolling(25).mean().iloc[-1])
    return ("above" if close >= sma25 else "below"), close, sma25


def topix_5d_return(topix: pd.Series, asof: pd.Timestamp):
    """エントリー日（asof 以降の最初の営業日）から 5 営業日の TOPIX ETF リターン [%]。"""
    s = topix[topix.index >= asof]
    if len(s) < 6:
        return None
    return float((s.iloc[5] / s.iloc[0] - 1) * 100)


def build_judged_df(records: list, prices: dict, topix: pd.Series) -> pd.DataFrame:
    """buy/sell 全件を判定し、分析用属性を付加した DataFrame を返す。"""
    rows = []
    for r in records:
        sig = r["signal"]
        if sig["signal"] == "hold":
            continue
        ohlcv = prices.get(r["code"])
        if ohlcv is None:
            continue
        res = judge_direction(r, ohlcv)
        if not res.get("judged"):
            continue

        pos, close_asof, sma25 = sma25_position(ohlcv, pd.Timestamp(r["price_as_of"]))
        entry, target, stop = res["entry"], res["target"], res["stop"]
        rr = None
        target_pct = stop_pct = None
        if target is not None and stop is not None and entry:
            if res["action"] == "buy":
                up, down = target - entry, entry - stop
            else:
                up, down = entry - target, stop - entry
            target_pct = 100 * up / entry
            stop_pct = 100 * down / entry
            rr = up / down if down > 0 else None
        reasons_text = json.dumps(sig.get("reasons", []), ensure_ascii=False)

        rows.append({
            **{k: res[k] for k in ("asof", "code", "action", "confidence", "entry",
                                   "target", "stop", "holding_period_days",
                                   "outcome", "hit", "truncated")},
            "month": r["asof"][:7],
            "model": extract_model(r["generator"]),
            "sma25_pos": pos,
            "close_asof": close_asof,
            "sma25": sma25,
            "topix_5d_pct": topix_5d_return(topix, pd.Timestamp(r["asof"])),
            "rr": rr,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "cites_fundamentals": bool(FUND_PATTERN.search(reasons_text)),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 集計ヘルパー
# ---------------------------------------------------------------------------

def hit_table(df: pd.DataFrame, by, order=None) -> pd.DataFrame:
    """切り口 by ごとの n / 的中数 / 的中率% のテーブル。"""
    g = df.groupby(by, dropna=False)["hit"].agg(n="count", hits="sum")
    g["hit_rate_pct"] = (100 * g["hits"] / g["n"]).round(1)
    if order is not None:
        g = g.reindex([o for o in order if o in g.index])
    return g.reset_index()


def md_table(df: pd.DataFrame, headers: list) -> list:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---:" if i else "---" for i in range(len(headers))) + "|"]
    for i in range(len(df)):
        cells = []
        for j in range(df.shape[1]):
            v = df.iat[i, j]  # iat で列ごとの dtype を保つ（iterrows は float に潰れる）
            if pd.isna(v):
                cells.append("-")
            elif isinstance(v, float):
                cells.append(f"{v:.1f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def conf_band(c: float) -> str:
    if c < 60:
        return "<60"
    if c < 70:
        return "60-69"
    return "70+"


def hpd_band(d: float) -> str:
    return "short(<=30日)" if d <= 30 else "long(>30日)"


def rr_band(rr) -> str:
    if rr is None or pd.isna(rr):
        return "target/stop 欠損"
    if rr < 1.0:
        return "<1.0"
    if rr < 1.5:
        return "1.0-1.5"
    if rr < 2.5:
        return "1.5-2.5"
    return ">=2.5"


# ---------------------------------------------------------------------------
# hold の機会損失
# ---------------------------------------------------------------------------

HOLD_UP_TH = 0.05    # 「大きく上げた」閾値 +5%
HOLD_DOWN_TH = 0.05  # 先に -5% を付けたら見逃しではない（buy でも stop 相当）
DEFAULT_HOLD_DAYS = 30


def analyze_holds(records: list, prices: dict) -> dict:
    """hold とした銘柄・日について、その後 holding 期間相当の値動きを集計する。

    - missed_buy: エントリー(as-of 寄り付き)から期間内に -5% より先に +5% に到達
      （buy を出していれば target 相当に届いたケース = 見逃し）
    - 参考として最大上昇率の分布と、+3%/+5%/+10% 到達率も返す
    """
    rows = []
    for r in records:
        sig = r["signal"]
        if sig["signal"] != "hold":
            continue
        ohlcv = prices.get(r["code"])
        if ohlcv is None:
            continue
        asof = pd.Timestamp(r["asof"])
        window = ohlcv[ohlcv.index >= asof]
        if window.empty:
            continue
        entry = float(window["Open"].iloc[0])
        hpd = sig.get("holding_period_days") or DEFAULT_HOLD_DAYS
        deadline = asof + pd.Timedelta(days=hpd)
        truncated = ohlcv.index.max() < deadline
        window = window[window.index <= deadline]

        missed = False
        for _, bar in window.iterrows():
            up = bar["High"] >= entry * (1 + HOLD_UP_TH)
            down = bar["Low"] <= entry * (1 - HOLD_DOWN_TH)
            if down:          # 同一バーで両方 → 保守的に見逃し扱いにしない
                break
            if up:
                missed = True
                break
        max_gain = float(window["High"].max() / entry - 1)
        end_ret = float(window["Close"].iloc[-1] / entry - 1)
        rows.append({"code": r["code"], "asof": r["asof"], "month": r["asof"][:7],
                     "hpd": hpd, "truncated": truncated, "missed_buy": missed,
                     "max_gain_pct": 100 * max_gain, "end_ret_pct": 100 * end_ret})
    df = pd.DataFrame(rows)
    return {
        "df": df,
        "n": len(df),
        "n_truncated": int(df["truncated"].sum()),
        "missed_rate_pct": round(100 * df["missed_buy"].mean(), 1),
        "up3_pct": round(100 * (df["max_gain_pct"] >= 3).mean(), 1),
        "up5_pct": round(100 * (df["max_gain_pct"] >= 5).mean(), 1),
        "up10_pct": round(100 * (df["max_gain_pct"] >= 10).mean(), 1),
        "end_up_pct": round(100 * (df["end_ret_pct"] > 0).mean(), 1),
        "median_max_gain_pct": round(float(df["max_gain_pct"].median()), 1),
    }


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

def rate(df: pd.DataFrame) -> float:
    return round(100 * df["hit"].mean(), 1) if len(df) else float("nan")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals-dir", type=Path, default=DEFAULT_SIGNALS_DIR,
                        help=f"分析対象シグナルのディレクトリ（既定: {DEFAULT_SIGNALS_DIR}）")
    parser.add_argument("--report", type=Path, default=None,
                        help="レポート出力先（既定: signals-dir 名から自動決定）")
    args = parser.parse_args()
    report_path = args.report or default_report_path(args.signals_dir)

    records = load_signal_records(args.signals_dir)
    prices = load_prices_yf()  # start=None: SMA25 計算のため全履歴を使う
    macro = _load_macro_history()
    topix = (macro[macro["ticker"] == TOPIX_TICKER]
             .set_index("Date").sort_index()["Close"])

    df = build_judged_df(records, prices, topix)
    df["confidence"] = df["confidence"].astype(int)
    df["holding_period_days"] = df["holding_period_days"].astype(int)
    df["conf_band"] = df["confidence"].map(conf_band)
    df["hpd_band"] = df["holding_period_days"].map(hpd_band)
    df["rr_band"] = df["rr"].map(rr_band)
    df["model_lite"] = df["model"].map(
        lambda m: "unknown" if m == "gemini" else ("lite" if "lite" in m else "non-lite"))

    buys = df[df["action"] == "buy"]
    sells = df[df["action"] == "sell"]
    holds = analyze_holds(records, prices)

    L = []
    L.append(f"# 敗因分析: ヒストリカルシミュレーション 方向的中率 {rate(df)}% (n={len(df)})")
    L.append("")
    L.append(f"- 生成日時: {datetime.now(JST).isoformat(timespec='seconds')}")
    L.append(f"- シグナルディレクトリ: {Path(args.signals_dir).name}")
    L.append("- 生成スクリプト: `poc/poc3_llm_signal_quality/historical/analyze_failures.py`"
             "（evaluate_historical.py の judge_direction を再利用。LLM API 不使用）")
    L.append("- 的中の定義: holding_period_days（暦日）以内に target 到達=的中 / "
             "stop 到達=外れ / 同一バーで両到達=外れ（保守）/ 期限切れ=含み損益の方向。"
             "エントリーは as-of 日の寄り付き。")
    L.append(f"- 判定対象: {len(df)} 件（buy {len(buys)} / sell {len(sells)}）、"
             f"全体的中率 {rate(df)}%")
    L.append("")

    # --- 1. 切り口別分解 -------------------------------------------------
    L.append("## 1. 的中率の切り口別分解")
    L.append("")
    L.append("### 1-1. 銘柄別")
    L.append("")
    t = hit_table(df, "code").sort_values("hit_rate_pct")
    L += md_table(t, ["code", "n", "的中", "的中率%"])
    L.append("")
    L.append("### 1-2. 月別 (as-of)")
    L.append("")
    t = hit_table(df, "month")
    L += md_table(t, ["月", "n", "的中", "的中率%"])
    L.append("")
    L.append("### 1-3. confidence 帯別")
    L.append("")
    t = hit_table(df, "conf_band", order=["<60", "60-69", "70+"])
    L += md_table(t, ["confidence 帯", "n", "的中", "的中率%"])
    L.append("")
    L.append("注: buy/sell の confidence は 65〜85 に集中しており <60 帯の buy/sell は 0 件。")
    L.append("")
    L.append("### 1-4. 生成モデル別")
    L.append("")
    t = hit_table(df, "model").sort_values("n", ascending=False)
    L += md_table(t, ["モデル", "n", "的中", "的中率%"])
    L.append("")
    t = hit_table(df, "model_lite", order=["lite", "non-lite", "unknown"])
    L += md_table(t, ["lite/非lite", "n", "的中", "的中率%"])
    L.append("")
    L.append("注: `gemini`（モデル名記録なし）は unknown として lite/非lite 比較から除外。")
    L.append("")
    L.append("月による交絡の確認（月内で lite / non-lite を比較）:")
    L.append("")
    cross = (df[df["model_lite"] != "unknown"]
             .groupby(["month", "model_lite"])["hit"].agg(n="count", hits="sum")
             .reset_index())
    cross["hit_rate_pct"] = (100 * cross["hits"] / cross["n"]).round(1)
    L += md_table(cross, ["月", "lite/非lite", "n", "的中", "的中率%"])
    L.append("")
    L.append("→ 同一月内でも non-lite が lite を一貫して上回っており、"
             "月（地合い）の交絡だけでは説明できない。")
    L.append("")
    L.append("### 1-5. 保有期間の長短別")
    L.append("")
    t = hit_table(df, "hpd_band", order=["short(<=30日)", "long(>30日)"])
    L += md_table(t, ["保有期間帯", "n", "的中", "的中率%"])
    L.append("")
    t = hit_table(df, "holding_period_days")
    L += md_table(t, ["holding_period_days", "n", "的中", "的中率%"])
    L.append("")

    # --- 2. confidence 較正 ----------------------------------------------
    L.append("## 2. confidence の較正")
    L.append("")
    L.append("confidence が高いほど的中率が高い（右上がり）なら較正が取れている。")
    L.append("")
    t = hit_table(df, "confidence")
    L += md_table(t, ["confidence", "n", "的中", "的中率%"])
    L.append("")
    corr = df[["confidence", "hit"]].astype(float).corr().iloc[0, 1]
    L.append(f"- confidence と的中 (0/1) の相関係数: {corr:.3f}")
    hi, lo = df[df["confidence"] >= 75], df[df["confidence"] < 75]
    L.append(f"- confidence>=75: 的中率 {rate(hi)}% (n={len(hi)}) / "
             f"<75: {rate(lo)}% (n={len(lo)})")
    mode_conf = df["confidence"].mode().iloc[0]
    mode_df = df[df["confidence"] == mode_conf]
    L.append(f"- 最頻値 confidence={mode_conf:.0f} (n={len(mode_df)}, 判定対象の"
             f"{100 * len(mode_df) / len(df):.0f}%) の的中率は {rate(mode_df)}% と"
             "全帯で最低。65/70/85 はいずれもこれを上回り、単調な右上がりではない。")
    L.append(f"- 結論: **較正できていない**（相関 {corr:.3f}）。confidence={mode_conf:.0f} が"
             "デフォルト値的に乱発されており、--min-confidence による足切りは"
             f"むしろ逆効果（>=75 で {rate(hi)}% < 75 未満の {rate(lo)}%）。")
    L.append("")

    # --- 3. buy 失敗の型分類 ----------------------------------------------
    stop_buys = buys[buys["outcome"].isin(["stop_hit", "target_and_stop_same_bar"])]
    L.append("## 3. buy 失敗の型分類")
    L.append("")
    L.append(f"buy {len(buys)} 件のうち stop 到達（同一バー両到達含む）は "
             f"{len(stop_buys)} 件"
             + (f"（{100 * len(stop_buys) / len(buys):.1f}%）。" if len(buys) else "。"))
    L.append("")
    L.append("### 3-a. エントリー時点の 25日線に対する位置（逆張り vs 順張り）")
    L.append("")
    L.append("判定: シグナル生成時に見えていた最終バー（price_as_of）の終値 vs 25日SMA。")
    L.append("")
    t = hit_table(buys, "sma25_pos", order=["above", "below"])
    L += md_table(t, ["25日線に対する位置", "n", "的中", "的中率%"])
    L.append("")
    sb = stop_buys["sma25_pos"].value_counts()
    L.append(f"- stop 到達 buy {len(stop_buys)} 件の内訳: "
             f"25日線より下（逆張り）{sb.get('below', 0)} 件 / "
             f"上（順張り）{sb.get('above', 0)} 件")
    L.append("")
    L.append("### 3-b. エントリー直後 5 営業日の市場（TOPIX ETF 1306.T）方向")
    L.append("")
    b2 = buys.dropna(subset=["topix_5d_pct"]).copy()
    b2["topix_dir"] = b2["topix_5d_pct"].map(lambda x: "TOPIX上昇" if x > 0 else "TOPIX下落")
    t = hit_table(b2, "topix_dir", order=["TOPIX上昇", "TOPIX下落"])
    L += md_table(t, ["直後5日の市場", "n", "的中", "的中率%"])
    L.append("")
    sb2 = stop_buys.dropna(subset=["topix_5d_pct"])
    n_dn = int((sb2["topix_5d_pct"] <= 0).sum())
    pct_dn = f"（{100 * n_dn / len(sb2):.1f}%）" if len(sb2) else ""
    L.append(f"- stop 到達 buy のうち TOPIX 5日方向が判定可能な {len(sb2)} 件中、"
             f"TOPIX 下落局面でのエントリーは {n_dn} 件{pct_dn}")
    L.append("")
    L.append("### 3-c. target/stop の非対称性（リスクリワード比）")
    L.append("")
    L.append("RR = (target − entry) / (entry − stop)。buy のみ。")
    L.append("")
    b3 = buys.dropna(subset=["rr"])
    q = b3["rr"].quantile([0.25, 0.5, 0.75])
    L.append(f"- RR 分布 (n={len(b3)}): 25%点 {q[0.25]:.2f} / 中央値 {q[0.5]:.2f} / "
             f"75%点 {q[0.75]:.2f}")
    L.append(f"- target 距離の中央値 +{b3['target_pct'].median():.1f}% / "
             f"stop 距離の中央値 −{b3['stop_pct'].median():.1f}%")
    L.append(f"- 的中群の RR 中央値 {b3[b3['hit']]['rr'].median():.2f} / "
             f"外れ群 {b3[~b3['hit']]['rr'].median():.2f}")
    L.append("")
    t = hit_table(b3, "rr_band", order=["<1.0", "1.0-1.5", "1.5-2.5", ">=2.5"])
    L += md_table(t, ["RR 帯", "n", "的中", "的中率%"])
    L.append("")
    L.append("### 3-d. 理由に fundamentals 引用があるか")
    L.append("")
    b4 = buys.copy()
    b4["fund"] = b4["cites_fundamentals"].map(
        lambda x: "fundamentals 引用あり" if x else "引用なし")
    t = hit_table(b4, "fund")
    L += md_table(t, ["理由の fundamentals 引用", "n", "的中", "的中率%"])
    L.append("")

    # --- 4. sell の失敗分析 -----------------------------------------------
    L.append("## 4. sell の失敗分析 (n が小さい点に注意)")
    L.append("")
    L.append(f"sell {len(sells)} 件、的中率 {rate(sells)}%。全件明細:")
    L.append("")
    sd = sells[["asof", "code", "confidence", "model", "sma25_pos",
                "holding_period_days", "rr", "outcome", "hit"]].sort_values("asof")
    L += md_table(sd, ["asof", "code", "conf", "モデル", "25日線位置",
                       "保有日数", "RR", "outcome", "的中"])
    L.append("")
    n_below = int((sells["sma25_pos"] == "below").sum())
    L.append(f"- 25日線より下（下落トレンド中）の sell: {n_below}/{len(sells)} 件")
    L.append(f"- stop 到達: {int((sells['outcome'] == 'stop_hit').sum())} 件 / "
             f"期限切れ判定: {int(sells['outcome'].str.startswith('expired').sum())} 件")
    L.append("")

    # --- 5. hold の機会損失 -----------------------------------------------
    L.append("## 5. hold の機会損失（buy を出せなかった見逃し率の推定）")
    L.append("")
    L.append(f"hold {holds['n']} 件について、as-of 寄り付きエントリーを仮定し "
             "holding_period_days（未指定時 30 暦日）内の値動きを集計。")
    L.append("")
    L.append(f"- **見逃し率（−5% より先に +5% 到達）: {holds['missed_rate_pct']}%**")
    L.append(f"- 期間内最大上昇率が +3% 以上: {holds['up3_pct']}% / "
             f"+5% 以上: {holds['up5_pct']}% / +10% 以上: {holds['up10_pct']}%")
    L.append(f"- 期間末終値がエントリーより上: {holds['end_up_pct']}%")
    L.append(f"- 最大上昇率の中央値: +{holds['median_max_gain_pct']}%")
    L.append(f"- うち期限が価格データ末尾を超過（打ち切り）: {holds['n_truncated']} 件"
             "（打ち切り分は見逃し率を過小評価する方向）")
    L.append("")
    hm = holds["df"].groupby("month")["missed_buy"].agg(n="count", missed="sum")
    hm["missed_rate_pct"] = (100 * hm["missed"] / hm["n"]).round(1)
    L += md_table(hm.reset_index(), ["月", "n", "見逃し", "見逃し率%"])
    L.append("")
    L.append(f"注: buy の的中率 {rate(buys)}% と hold の見逃し率 "
             f"{holds['missed_rate_pct']}% がほぼ同水準。判定基準は異なる"
             "（buy はモデル自身の target/stop、hold は対称 ±5%）ものの、"
             "buy の選別が「無差別にエントリーした場合のベースレート」と"
             "大差ない可能性を示唆する。")
    L.append("")

    # --- 6. truncated の扱い ----------------------------------------------
    L.append("## 6. truncated（期限が価格データ末尾を超過）の扱い")
    L.append("")
    n_trunc_all = int(df["truncated"].sum())
    exp_trunc = df[df["outcome"] == "expired_truncated"]
    L.append(f"- truncated フラグ付き: {n_trunc_all} 件"
             f"（うち target/stop 未到達のまま最終バー判定 = expired_truncated: "
             f"{len(exp_trunc)} 件、その的中率 {rate(exp_trunc)}%）")
    ex1 = df[df["outcome"] != "expired_truncated"]
    ex2 = df[~df["truncated"]]
    L.append(f"- expired_truncated {len(exp_trunc)} 件を除外した的中率: "
             f"**{rate(ex1)}%** (n={len(ex1)})")
    L.append(f"- truncated 全 {n_trunc_all} 件を除外した的中率: "
             f"{rate(ex2)}% (n={len(ex2)})")
    L.append("")

    # --- 7. 改善仮説 ------------------------------------------------------
    L.append("## 7. 改善仮説の候補")
    L.append("")
    below = buys[buys["sma25_pos"] == "below"]
    above = buys[buys["sma25_pos"] == "above"]
    tp_dn = b2[b2["topix_5d_pct"] <= 0]
    tp_up = b2[b2["topix_5d_pct"] > 0]
    rr_mid = b3[(b3["rr"] >= 1.0) & (b3["rr"] < 1.5)]
    rr_top = b3[b3["rr"] >= 2.5]
    n_rr_top_stop = int((rr_top["outcome"] == "stop_hit").sum())
    lite = df[df["model_lite"] == "lite"]
    nonlite = df[df["model_lite"] == "non-lite"]
    fund_y = buys[buys["cites_fundamentals"]]
    fund_n = buys[~buys["cites_fundamentals"]]

    hyps = [
        f"**地合い（市場トレンド）フィルタの導入**: エントリー直後 5 日の TOPIX が"
        f"下落した局面の buy は的中率 {rate(tp_dn)}% (n={len(tp_dn)})、上昇局面は "
        f"{rate(tp_up)}% (n={len(tp_up)})。stop 到達 buy "
        f"{len(stop_buys)} 件の "
        f"{(100 * n_dn / len(sb2)) if len(sb2) else float('nan'):.0f}% が TOPIX 下落局面での"
        f"エントリーで、月別でも地合いが崩れた月が低い傾向（1-2 節参照）。"
        "個別材料でなく市場ベータで負けている → TOPIX/日経の短期トレンドが下向きの"
        "ときは buy を抑制する（またはサイズを落とす）ルールを追加する。",
        f"**target の欲張り抑制（RR 上限の設定）**: RR>=2.5 の buy は的中率 "
        f"{rate(rr_top)}% (n={len(rr_top)}、うち stop_hit {n_rr_top_stop} 件) で、"
        f"RR 1.0-1.5 の {rate(rr_mid)}% (n={len(rr_mid)}) を大きく下回る。"
        f"的中群の RR 中央値 {b3[b3['hit']]['rr'].median():.2f} < 外れ群 "
        f"{b3[~b3['hit']]['rr'].median():.2f}。target 距離中央値 "
        f"+{b3['target_pct'].median():.1f}% は保有 30-45 日に対して過大で、"
        "先に stop（中央値 −6.3%）に狩られる → target/stop の整合チェック"
        "（RR 1.0〜1.5 目安、ATR 連動の stop 幅）をプロンプトまたは後段"
        "バリデーションに追加する。",
        f"**非 lite モデルでの再実行**: lite 系 {rate(lite)}% (n={len(lite)}) vs "
        f"非 lite {rate(nonlite)}% (n={len(nonlite)})。同一月内の比較でも非 lite が"
        "一貫して上回る（1-4 節）ため、判定対象の 74% を占める "
        "gemini-3.1-flash-lite が全体の的中率を押し下げている可能性が高い → "
        "gemini-2.5-flash / 3-flash-preview 等の非 lite で同一期間を再実行して検証する。",
        f"**sell シグナルの停止または要件強化**: sell は的中率 {rate(sells)}% "
        f"(n={len(sells)}) と極端に低く、{n_below}/{len(sells)} 件が 25 日線より下"
        "（すでに下げた後）で出ており、戻り局面で stop に狩られている"
        f"（stop_hit {int((sells['outcome'] == 'stop_hit').sum())}/{len(sells)}）→ "
        "sell は当面評価対象から外すか、明確な悪材料（開示・決算）を必須条件にする。",
        f"**buy 根拠の強化（fundamentals 引用の必須化・逆張り抑制）**: 理由に "
        f"fundamentals を引用した buy は {rate(fund_y)}% (n={len(fund_y)}) vs "
        f"引用なし {rate(fund_n)}% (n={len(fund_n)})。また 25 日線より下での"
        f"逆張り buy は {rate(below)}% (n={len(below)}) と 25 日線より上の "
        f"{rate(above)}% (n={len(above)}) を下回る（差は小さめ）→ buy には "
        "fundamentals 根拠を必須とし、25 日線より下の逆張り buy には追加の"
        "根拠（悪材料出尽くし等）を要求する。",
    ]
    for i, h in enumerate(hyps, 1):
        L.append(f"{i}. {h}")
    L.append("")
    L.append("補足（仮説化を見送った切り口）: 保有期間は long(>30日) "
             f"{rate(df[df['hpd_band'] == 'long(>30日)'])}% vs short(<=30日) "
             f"{rate(df[df['hpd_band'] == 'short(<=30日)'])}% と長い方がむしろ高く、"
             "「保有期間短縮」はデータの裏付けなし。confidence は較正されておらず"
             "（2 節）、足切り・サイズ調整に使うのは現状では逆効果。")
    L.append("")

    report_path.write_text("\n".join(L), encoding="utf-8")
    print(f"レポート出力: {report_path}")

    # コンソールに要点を出す
    print(f"\n全体的中率 {rate(df)}% (n={len(df)})")
    print(f"buy {rate(buys)}% (n={len(buys)}) / sell {rate(sells)}% (n={len(sells)})")
    print(f"25日線下 buy {rate(below)}% (n={len(below)}) / 上 {rate(above)}% (n={len(above)})")
    print(f"TOPIX下落局面 buy {rate(tp_dn)}% (n={len(tp_dn)}) / 上昇 {rate(tp_up)}% (n={len(tp_up)})")
    print(f"confidence 相関 {corr:.3f}")
    print(f"hold 見逃し率 {holds['missed_rate_pct']}% (n={holds['n']})")
    print(f"expired_truncated 除外 {rate(ex1)}% (n={len(ex1)}) / truncated 全除外 {rate(ex2)}% (n={len(ex2)})")


if __name__ == "__main__":
    main()
