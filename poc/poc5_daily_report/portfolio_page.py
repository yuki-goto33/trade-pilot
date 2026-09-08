"""資産推移ページ（docs/portfolio.html / portfolio.json）の生成。

signals_history 全期間を simulate.py でリプレイし、TOPIX 連動 ETF（1306.T）に
初期資金を全額投資した場合と並べて公開する。チャートは html_report.py と同じ
自己完結インライン SVG（外部ライブラリなし・無料配信）。

単体実行: ../../.venv/bin/python portfolio_page.py
"""
import html
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from simulate import (SimulationResult, fetch_ohlc, load_signal_history,
                      run_simulation)

POC_DIR = Path(__file__).resolve().parent
REPO_ROOT = POC_DIR.parents[1]
SIGNALS_HISTORY_DIR = POC_DIR / "signals_history"
DOCS_DIR = REPO_ROOT / "docs"

INITIAL_CASH = 3_000_000
ALLOCATION = 0.10
BENCHMARK_TICKER = "1306.T"  # NEXT FUNDS TOPIX 連動型上場投信

JST = timezone(timedelta(hours=9))


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def fmt_yen(v) -> str:
    return f"{v:,.0f}円"


def build_benchmark(bench_df, dates: list, initial_cash: float) -> list:
    """シミュレーションと同じ日付列で、初日終値に全額投資した場合の推移。"""
    if bench_df is None or not dates:
        return []
    closes = {d.date().isoformat(): float(row["Close"])
              for d, row in bench_df.iterrows()}
    base = None
    last = None
    out = []
    for date in dates:
        c = closes.get(date, last)
        if c is None:
            continue
        last = c
        if base is None:
            base = c
        out.append({"date": date, "equity": round(initial_cash * c / base, 2)})
    return out


def build_equity_chart_svg(daily: list, benchmark: list,
                           width=880, height=300) -> str:
    """資産推移（青）と TOPIX ベンチマーク（グレー破線）の 2 本線チャート。"""
    if len(daily) < 2:
        return ""
    bench_map = {b["date"]: b["equity"] for b in benchmark}
    dates = [d["date"] for d in daily]
    equity = [d["equity"] for d in daily]
    bench = [bench_map.get(d) for d in dates]

    pad_l, pad_r, pad_t, pad_b = 74, 10, 26, 22
    n = len(dates)
    ys = equity + [v for v in bench if v is not None]
    y_min, y_max = min(ys), max(ys)
    span = (y_max - y_min) or 1
    y_min -= span * 0.06
    y_max += span * 0.06

    def x(i):
        return pad_l + (width - pad_l - pad_r) * i / max(n - 1, 1)

    def y(v):
        return pad_t + (height - pad_t - pad_b) * (1 - (v - y_min) / (y_max - y_min))

    def polyline(vals, color, w="2", dash=""):
        seg, segs = [], []
        for i, v in enumerate(vals):
            if v is None:
                if seg:
                    segs.append(seg)
                    seg = []
                continue
            seg.append(f"{x(i):.1f},{y(v):.1f}")
        if seg:
            segs.append(seg)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        return "".join(
            f'<polyline fill="none" stroke="{color}" stroke-width="{w}"{d} '
            f'points="{" ".join(s)}"/>' for s in segs if len(s) >= 2)

    el = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
          f'viewBox="0 0 {width} {height}" role="img">',
          f'<rect width="{width}" height="{height}" fill="#ffffff"/>']

    # y 軸グリッド + ラベル（万円）
    for k in range(5):
        gy = pad_t + (height - pad_t - pad_b) * k / 4
        gv = y_max - (y_max - y_min) * k / 4
        el.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
                  f'stroke="#eef2f6" stroke-width="1"/>')
        el.append(f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" text-anchor="end" '
                  f'font-size="10.5" fill="#7b8794">{gv / 10000:,.0f}万円</text>')

    # 初期資金の基準線
    if y_min <= INITIAL_CASH <= y_max:
        el.append(f'<line x1="{pad_l}" y1="{y(INITIAL_CASH):.1f}" x2="{width - pad_r}" '
                  f'y2="{y(INITIAL_CASH):.1f}" stroke="#cbd5e1" stroke-width="1" '
                  f'stroke-dasharray="2,3"/>')

    # x 軸ラベル（約6分割）
    step = max(n // 6, 1)
    for i in range(0, n, step):
        el.append(f'<text x="{x(i):.1f}" y="{height - 6}" text-anchor="middle" '
                  f'font-size="10" fill="#7b8794">{dates[i][5:]}</text>')

    el.append(polyline(bench, "#94a3b8", "1.6", dash="5,4"))
    el.append(polyline(equity, "#0b62c4", "2.2"))

    # 凡例
    el.append(f'<line x1="{pad_l}" y1="12" x2="{pad_l + 22}" y2="12" '
              f'stroke="#0b62c4" stroke-width="2.2"/>')
    el.append(f'<text x="{pad_l + 27}" y="16" font-size="11" fill="#334155">trade-pilot</text>')
    el.append(f'<line x1="{pad_l + 110}" y1="12" x2="{pad_l + 132}" y2="12" '
              f'stroke="#94a3b8" stroke-width="1.6" stroke-dasharray="5,4"/>')
    el.append(f'<text x="{pad_l + 137}" y="16" font-size="11" fill="#334155">'
              f'TOPIX（1306 全額投資）</text>')
    el.append("</svg>")
    return "".join(el)


def compute_stats(result: SimulationResult) -> dict:
    trades = result.trades
    wins = sum(1 for t in trades if t["pnl"] > 0)
    realized = sum(t["pnl"] for t in trades)
    unrealized = sum(
        p["value"] - p["entry_price"] * p["shares"] for p in result.positions)
    equities = [d["equity"] for d in result.daily]
    max_dd = 0.0
    peak = equities[0] if equities else 0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100)
    return {
        "n_trades": len(trades),
        "n_wins": wins,
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else None,
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def build_portfolio_json(result: SimulationResult, benchmark: list) -> dict:
    latest = result.daily[-1] if result.daily else {
        "date": None, "equity": result.initial_cash}
    return_pct = (round((latest["equity"] / result.initial_cash - 1) * 100, 2)
                  if result.initial_cash else 0.0)
    return {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "initial_cash": result.initial_cash,
        "latest": {"date": latest.get("date"), "equity": latest["equity"],
                   "return_pct": return_pct},
        "stats": compute_stats(result),
        "daily": result.daily,
        "benchmark": benchmark,
        "positions": result.positions,
        "trades": result.trades,
    }


EXIT_REASON_LABELS = {
    "target": "目標到達",
    "stop_loss": "損切り",
    "holding_period": "期間満了",
    "sell_signal": "sellシグナル",
}


def _pnl_span(v: float, suffix="") -> str:
    cls = "pos" if v >= 0 else "neg"
    sign = "+" if v >= 0 else ""
    return f'<span class="{cls}">{sign}{v:,.2f}{suffix}</span>'


def build_portfolio_html(result: SimulationResult, benchmark: list) -> str:
    data = build_portfolio_json(result, benchmark)
    latest, stats = data["latest"], data["stats"]
    updated = data["generated_at"][:10]

    chart = build_equity_chart_svg(result.daily, benchmark)
    chart_html = (f'<div class="chart">{chart}</div>' if chart
                  else '<p class="sub">データなし（シグナル蓄積待ち）</p>')

    bench_last = benchmark[-1]["equity"] if benchmark else None
    bench_pct = (round((bench_last / result.initial_cash - 1) * 100, 2)
                 if bench_last else None)

    cards = [
        ("現在資産", fmt_yen(latest["equity"]), None),
        ("累計損益", f'{_pnl_span(latest["return_pct"], "%")}', None),
        ("TOPIX（同期間）", _pnl_span(bench_pct, "%") if bench_pct is not None else "—", None),
        ("決済取引", f'{stats["n_trades"]}件', None),
        ("勝率", f'{stats["win_rate_pct"]}%' if stats["win_rate_pct"] is not None else "—", None),
        ("最大DD", f'-{stats["max_drawdown_pct"]}%', None),
    ]
    cards_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{esc(k)}</div>'
        f'<div class="kpi-value">{v}</div></div>' for k, v, _ in cards)

    pos_rows = "".join(
        f'<tr><td>{esc(p["code"])} {esc(p["name"])}</td>'
        f'<td>{esc(p["entry_date"])}</td>'
        f'<td class="num">{p["entry_price"]:,.1f}</td>'
        f'<td class="num">{p["last_price"]:,.1f}</td>'
        f'<td class="num">{fmt_yen(p["value"])}</td>'
        f'<td class="num">{_pnl_span(p["unrealized_pnl_pct"], "%")}</td></tr>'
        for p in result.positions)
    positions_html = (
        f'<table><thead><tr><th>銘柄</th><th>取得日</th><th>取得単価</th>'
        f'<th>現在値</th><th>評価額</th><th>評価損益</th></tr></thead>'
        f'<tbody>{pos_rows}</tbody></table>' if pos_rows
        else '<p class="sub">現在保有はありません。</p>')

    trade_rows = "".join(
        f'<tr><td>{esc(t["code"])} {esc(t["name"])}</td>'
        f'<td>{esc(t["entry_date"])}</td><td>{esc(t["exit_date"])}</td>'
        f'<td class="num">{t["entry_price"]:,.1f}</td>'
        f'<td class="num">{t["exit_price"]:,.1f}</td>'
        f'<td>{esc(EXIT_REASON_LABELS.get(t["exit_reason"], t["exit_reason"]))}</td>'
        f'<td class="num">{_pnl_span(t["pnl_pct"], "%")}</td>'
        f'<td class="num">{_pnl_span(t["pnl"], "円")}</td></tr>'
        for t in sorted(result.trades, key=lambda t: t["exit_date"], reverse=True))
    trades_html = (
        f'<table><thead><tr><th>銘柄</th><th>取得日</th><th>決済日</th>'
        f'<th>取得単価</th><th>決済単価</th><th>決済理由</th><th>損益率</th>'
        f'<th>損益</th></tr></thead><tbody>{trade_rows}</tbody></table>'
        if trade_rows else '<p class="sub">決済済みの取引はまだありません。</p>')

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trade-pilot 資産推移シミュレーション</title>
<style>
body {{ font-family: 'Hiragino Sans', 'Noto Sans JP', 'Yu Gothic', sans-serif;
       margin: 0; background: #f4f6f8; color: #1a2733; line-height: 1.65; }}
.wrap {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }}
h1 {{ font-size: 24px; margin: 8px 0 4px; }}
h2 {{ font-size: 18px; margin: 28px 0 10px; }}
.sub {{ color: #5b6b7a; font-size: 13px; margin-bottom: 20px; }}
.sub a {{ color: #0b62c4; }}
.kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 16px 0; }}
@media (min-width: 760px) {{ .kpis {{ grid-template-columns: repeat(6, 1fr); }} }}
.kpi {{ background: #fff; border-radius: 10px; padding: 10px 12px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.kpi-label {{ font-size: 11.5px; color: #5b6b7a; }}
.kpi-value {{ font-size: 16.5px; font-weight: 700; margin-top: 2px; }}
.card {{ background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08);
         padding: 18px 20px; margin: 14px 0; }}
.chart {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #e8edf2; }}
th {{ color: #5b6b7a; font-weight: 600; font-size: 12px; white-space: nowrap; }}
td.num, th.num {{ text-align: right; }}
.pos {{ color: #16a34a; font-weight: 600; }}
.neg {{ color: #dc2626; font-weight: 600; }}
.assumptions {{ background: #fffbeb; border-radius: 10px; padding: 12px 16px;
                font-size: 12.5px; color: #7c5e10; }}
.assumptions ul {{ margin: 4px 0; padding-left: 18px; }}
</style></head><body><div class="wrap">
<h1>資産推移シミュレーション</h1>
<p class="sub">trade-pilot のシグナルに完全遵従して売買した場合の仮想資産推移
（{updated} 更新） ・ <a href="reports/index.html">デイリーレポート一覧へ</a></p>
<div class="kpis">{cards_html}</div>
<div class="card">{chart_html}</div>
<h2>現在ポジション</h2>
<div class="card">{positions_html}</div>
<h2>取引履歴</h2>
<div class="card">{trades_html}</div>
<h2>シミュレーションの前提</h2>
<div class="card assumptions"><ul>
<li>初期資金 {fmt_yen(INITIAL_CASH)}。buy シグナル当日の始値で現金残高の {ALLOCATION:.0%} を投入（端株可）。</li>
<li>出口: 損切り（安値タッチ）＞目標（高値タッチ）＞保有期間満了（翌営業日始値）。保有中の再 buy は無視。</li>
<li>手数料・スリッページ・税金はゼロと仮定。株式分割は株数を調整。</li>
<li>シグナルは前日終値までのデータで生成（先読みなし）。価格は yfinance の日足。</li>
<li>本ページは検証用シミュレーションであり、投資助言ではありません。</li>
</ul></div>
</div></body></html>
"""


def main() -> int:
    history = load_signal_history(SIGNALS_HISTORY_DIR)
    if not history:
        print("[NG] signals_history が空", file=sys.stderr)
        return 1
    codes = sorted({s["code"] for sigs in history.values() for s in sigs})
    start = min(history.keys())
    print(f"[..] 価格取得: {len(codes)}銘柄 + ベンチマーク（{start}〜）")
    ohlc = fetch_ohlc(codes + [BENCHMARK_TICKER], start)
    bench_df = ohlc.pop(BENCHMARK_TICKER, None)
    if bench_df is None:
        print("[WARN] ベンチマーク 1306.T の取得に失敗（比較線なしで続行）",
              file=sys.stderr)

    result = run_simulation(history, ohlc,
                            initial_cash=INITIAL_CASH, allocation=ALLOCATION)
    benchmark = build_benchmark(bench_df, [d["date"] for d in result.daily],
                                INITIAL_CASH)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "portfolio.json").write_text(
        json.dumps(build_portfolio_json(result, benchmark),
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    (DOCS_DIR / "portfolio.html").write_text(
        build_portfolio_html(result, benchmark), encoding="utf-8")
    latest = result.daily[-1] if result.daily else None
    print(f"[OK] docs/portfolio.html / portfolio.json 生成"
          f"（{latest['date'] if latest else '-'} 時点 equity="
          f"{latest['equity']:,.0f}円）" if latest else "[OK] 生成（データなし）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
