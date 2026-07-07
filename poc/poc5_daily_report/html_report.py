"""PoC-5: リッチ版デイリーレポート（自己完結 HTML）を構築する。

Markdown / Slack 版（report_builder.py）が「シグナルの要約」なのに対し、
HTML 版は銘柄ごとに以下を1ページで見せる:

- 株価チャート（直近60営業日の終値 + SMA5/25 + 目標/損切り/節目ライン、SVG 手描画）
- テクニカル専門家の見解（stance / strength / 根拠 + evidence / 注意点）
- ファンダメンタルズ専門家の見解（同上）
- チーフアナリストの総合判断（シグナル / 確信度 / 目標・損切り / 理由 / リスク）
- 参照ニュース・適時開示へのリンク（シグナルレコードの context_refs）

外部依存なし（matplotlib 不使用、SVG を直接組み立てる）。画像もCSSも
インライン化するため、生成された 1 ファイルだけで完結する。

単体実行:
    ../../.venv/bin/python html_report.py [--date YYYY-MM-DD] [--out out.html]
"""
import argparse
import csv
import html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report_builder import (
    DATA_DIR,
    load_macro_snapshot,
    load_signals,
    split_holds,
    split_records,
    today_jst,
)

JST = timezone(timedelta(hours=9))

CHART_BDAYS = 60  # チャートに描く営業日数

STANCE_JP = {"bullish": "強気", "bearish": "弱気", "neutral": "中立"}
STANCE_CLASS = {"bullish": "bull", "bearish": "bear", "neutral": "neut"}
SIGNAL_JP = {"buy": "買い", "sell": "売り", "hold": "様子見"}
SIGNAL_CLASS = {"buy": "buy", "sell": "sell", "hold": "hold"}

CSS = """
body { font-family: 'Hiragino Sans', 'Noto Sans JP', 'Yu Gothic', sans-serif;
       margin: 0; background: #f4f6f8; color: #1a2733; line-height: 1.65; }
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 24px; margin: 8px 0 4px; }
.sub { color: #5b6b7a; font-size: 13px; margin-bottom: 20px; }
.counts { display: inline-block; background: #fff; border-radius: 8px;
          padding: 6px 14px; font-size: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.stock { background: #fff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08);
         margin: 22px 0; padding: 20px 22px; }
.stock h2 { font-size: 19px; margin: 0 0 2px; }
.badge { display: inline-block; font-size: 12px; font-weight: 700; border-radius: 999px;
         padding: 2px 12px; margin-left: 8px; vertical-align: 2px; color: #fff; }
.badge.buy { background: #16a34a; } .badge.sell { background: #dc2626; }
.badge.hold { background: #64748b; } .badge.watch { background: #d97706; }
.meta-line { color: #5b6b7a; font-size: 13px; margin-bottom: 10px; }
.chart { margin: 8px 0 14px; overflow-x: auto; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 760px) { .cols { grid-template-columns: 1fr; } }
.card { border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px 14px; }
.card h3 { font-size: 14px; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }
.stance { font-size: 12px; font-weight: 700; border-radius: 999px; padding: 1px 10px; color: #fff; }
.stance.bull { background: #16a34a; } .stance.bear { background: #dc2626; }
.stance.neut { background: #94a3b8; }
.card ul { margin: 6px 0; padding-left: 18px; }
.card li { margin: 4px 0; font-size: 13.5px; }
.ev { color: #64748b; font-size: 11.5px; display: block; }
.cautions { background: #fef9ec; border-radius: 8px; padding: 8px 12px; margin-top: 8px;
            font-size: 12.5px; color: #7c5e10; }
.cautions ul { margin: 2px 0; padding-left: 16px; }
.synth { border: 2px solid #c7d6e8; background: #f8fbff; grid-column: 1 / -1; }
.kv { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13.5px; margin: 6px 0; }
.kv b { color: #0f2f57; }
.risks { background: #fdf2f2; border-radius: 8px; padding: 8px 12px; margin-top: 8px;
         font-size: 12.5px; color: #8a1f1f; }
.risks ul { margin: 2px 0; padding-left: 16px; }
.refs { margin-top: 12px; font-size: 13px; }
.refs h4 { font-size: 13px; margin: 10px 0 4px; color: #33475b; }
.refs ul { margin: 2px 0; padding-left: 18px; }
.refs a { color: #0b62c4; text-decoration: none; }
.refs a:hover { text-decoration: underline; }
.refs .dim { color: #7b8794; font-size: 11.5px; }
table.macro { border-collapse: collapse; background: #fff; border-radius: 10px;
              overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 13.5px; }
table.macro th, table.macro td { padding: 6px 14px; border-bottom: 1px solid #eef2f6; text-align: left; }
table.macro th { background: #eef4fa; }
.section-h { font-size: 17px; margin: 30px 0 6px; }
.legend { font-size: 11.5px; color: #5b6b7a; }
.footer { color: #8a97a4; font-size: 12px; margin-top: 30px; }
.missing { background: #fff7ed; border-radius: 8px; padding: 8px 14px; font-size: 13px;
           color: #9a3412; margin-top: 16px; }
"""


# ---------------------------------------------------------------------------
# 価格データ（チャート用）
# ---------------------------------------------------------------------------

def load_price_series() -> dict:
    """data/prices_yfinance.csv から code -> [{date, close, volume}, ...]（昇順）。"""
    path = DATA_DIR / "prices_yfinance.csv"
    series = {}
    if not path.is_file():
        return series
    per_ticker = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            per_ticker.setdefault(row["ticker"], []).append(row)
    for ticker, rows in per_ticker.items():
        rows.sort(key=lambda r: r["Date"])
        code = ticker.split(".")[0]
        pts = []
        for r in rows:
            try:
                pts.append({
                    "date": r["Date"][:10],
                    "close": float(r["Close"]),
                    "volume": float(r.get("Volume") or 0),
                })
            except (ValueError, KeyError):
                continue
        if pts:
            series[code] = pts
    return series


def _sma(values, n):
    out = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - n: i + 1]) / n)
    return out


# ---------------------------------------------------------------------------
# SVG チャート
# ---------------------------------------------------------------------------

def build_chart_svg(points: list, levels: dict, width=880, height=320) -> str:
    """終値 + SMA5/25 の折れ線と出来高バー、水平ライン（目標/損切り/節目）を描く。

    points: [{date, close, volume}, ...]（昇順）。levels: {ラベル: 価格} で
    None 値は無視。データ不足（10本未満）なら空文字。
    """
    closes_all = [p["close"] for p in points]
    sma5_all = _sma(closes_all, 5)
    sma25_all = _sma(closes_all, 25)
    pts = points[-CHART_BDAYS:]
    off = len(points) - len(pts)
    closes = closes_all[off:]
    sma5 = sma5_all[off:]
    sma25 = sma25_all[off:]
    if len(pts) < 10:
        return ""

    pad_l, pad_r, pad_t = 62, 10, 8
    price_h = height - 78          # 価格エリア
    vol_top = height - 62          # 出来高エリア上端
    vol_h = 44
    n = len(pts)

    level_items = [(k, v) for k, v in (levels or {}).items() if v]
    ys = closes + [v for v in sma5 + sma25 if v is not None]
    # 現値から ±20% 以内の水平ラインのみレンジに含める（外れ値でつぶれないように）
    last_close = closes[-1]
    for _, v in level_items:
        if abs(v / last_close - 1) <= 0.20:
            ys.append(v)
    y_min, y_max = min(ys), max(ys)
    span = (y_max - y_min) or 1
    y_min -= span * 0.06
    y_max += span * 0.06

    def x(i):
        return pad_l + (width - pad_l - pad_r) * i / max(n - 1, 1)

    def y(v):
        return pad_t + (price_h - pad_t) * (1 - (v - y_min) / (y_max - y_min))

    def polyline(vals, color, w="1.6", dash=""):
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
          f'viewBox="0 0 {width} {height}" role="img">']
    el.append(f'<rect width="{width}" height="{height}" fill="#ffffff"/>')

    # y 軸グリッド + ラベル
    for k in range(5):
        gy = pad_t + (price_h - pad_t) * k / 4
        gv = y_max - (y_max - y_min) * k / 4
        el.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
                  f'stroke="#eef2f6" stroke-width="1"/>')
        el.append(f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" text-anchor="end" '
                  f'font-size="10.5" fill="#7b8794">{gv:,.0f}</text>')

    # x 軸ラベル（約6分割）
    step = max(n // 6, 1)
    for i in range(0, n, step):
        el.append(f'<text x="{x(i):.1f}" y="{height - 4}" text-anchor="middle" '
                  f'font-size="10" fill="#7b8794">{pts[i]["date"][5:]}</text>')

    # 出来高バー
    vols = [p["volume"] for p in pts]
    v_max = max(vols) or 1
    bar_w = max((width - pad_l - pad_r) / n - 1.2, 0.8)
    for i, v in enumerate(vols):
        bh = vol_h * v / v_max
        el.append(f'<rect x="{x(i) - bar_w / 2:.1f}" y="{vol_top + vol_h - bh:.1f}" '
                  f'width="{bar_w:.1f}" height="{bh:.1f}" fill="#cbd5e1"/>')

    # 水平ライン（目標/損切り/節目）
    line_colors = {"目標": "#16a34a", "損切り": "#dc2626"}
    for label, v in level_items:
        if not (y_min <= v <= y_max):
            continue
        color = line_colors.get(label, "#94a3b8")
        el.append(f'<line x1="{pad_l}" y1="{y(v):.1f}" x2="{width - pad_r}" y2="{y(v):.1f}" '
                  f'stroke="{color}" stroke-width="1.2" stroke-dasharray="5,4"/>')
        el.append(f'<text x="{width - pad_r - 2}" y="{y(v) - 3:.1f}" text-anchor="end" '
                  f'font-size="10.5" fill="{color}">{html.escape(label)} {v:,.0f}</text>')

    # 折れ線（終値・SMA）
    el.append(polyline(sma25, "#8b5cf6", "1.4", dash="2,3"))
    el.append(polyline(sma5, "#f59e0b", "1.4"))
    el.append(polyline(closes, "#0b62c4", "2"))
    el.append("</svg>")
    return "".join(el)


# ---------------------------------------------------------------------------
# HTML 部品
# ---------------------------------------------------------------------------

def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def fmt_price(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{int(v):,}円" if v == int(v) else f"{v:,.1f}円"


def stance_span(view: dict) -> str:
    st = view.get("stance", "neutral")
    return (f'<span class="stance {STANCE_CLASS.get(st, "neut")}">'
            f'{STANCE_JP.get(st, st)} {view.get("strength", "")}</span>')


def expert_card(title: str, view: dict) -> str:
    if not view:
        return (f'<div class="card"><h3>{esc(title)}</h3>'
                f'<p class="dim">見解データなし（旧形式レコード）</p></div>')
    parts = [f'<div class="card"><h3>{esc(title)} {stance_span(view)}</h3>']
    if view.get("summary"):
        parts.append(f'<p style="font-size:13.5px;margin:4px 0">{esc(view["summary"])}</p>')
    points = view.get("points") or []
    if points:
        parts.append("<ul>")
        for p in points:
            parts.append(f'<li>{esc(p.get("point"))}'
                         f'<span class="ev">根拠: {esc(p.get("evidence"))}</span></li>')
        parts.append("</ul>")
    kl = view.get("key_levels") or {}
    if kl.get("support") or kl.get("resistance"):
        parts.append(f'<div class="kv"><span>支持線 <b>{fmt_price(kl.get("support"))}</b></span>'
                     f'<span>抵抗線 <b>{fmt_price(kl.get("resistance"))}</b></span></div>')
    cautions = view.get("cautions") or []
    if cautions:
        parts.append('<div class="cautions">⚠ 注意点<ul>')
        for c in cautions:
            parts.append(f"<li>{esc(c)}</li>")
        parts.append("</ul></div>")
    parts.append("</div>")
    return "".join(parts)


def synthesis_card(record: dict) -> str:
    s = record["signal"]
    sig = s["signal"]
    parts = ['<div class="card synth">']
    parts.append(f'<h3>チーフアナリスト（総合判断）'
                 f'<span class="stance {SIGNAL_CLASS[sig]}" style="background:#0f2f57">'
                 f'{SIGNAL_JP[sig]} / 確信度 {s["confidence"]}</span></h3>')
    kv = [f'<span>目標 <b>{fmt_price(s.get("target_price"))}</b></span>',
          f'<span>損切り <b>{fmt_price(s.get("stop_loss"))}</b></span>',
          f'<span>想定保有 <b>{s.get("holding_period_days", "—")}日</b></span>']
    rb = record.get("recent_buy")
    if rb:
        kv.append(f'<span>直近buy <b>{esc(rb.get("date"))}（{rb.get("days_ago")}日前）</b>'
                  ' → 再エントリー抑制中</span>')
    parts.append(f'<div class="kv">{"".join(kv)}</div>')
    reasons = s.get("reasons") or []
    if reasons:
        parts.append("<ul>")
        for r in reasons:
            parts.append(f'<li>{esc(r.get("reason"))}'
                         f'<span class="ev">根拠: {esc(r.get("evidence"))}</span></li>')
        parts.append("</ul>")
    risks = s.get("risks") or []
    if risks:
        parts.append('<div class="risks">⚠ リスク<ul>')
        for r in risks:
            parts.append(f"<li>{esc(r)}</li>")
        parts.append("</ul></div>")
    parts.append("</div>")
    return "".join(parts)


def refs_block(record: dict) -> str:
    refs = record.get("context_refs") or {}
    news = [n for n in (refs.get("news") or []) if n.get("title")]
    discl = [d for d in (refs.get("disclosures") or []) if d.get("title")]
    if not news and not discl:
        return ""
    parts = ['<div class="refs">']
    if news:
        parts.append("<h4>📰 参照ニュース（分析入力に含まれた見出し）</h4><ul>")
        for item in news:
            title = esc(item["title"])
            body = (f'<a href="{esc(item["url"])}" target="_blank" rel="noopener">{title}</a>'
                    if item.get("url") else title)
            dim = " / ".join(x for x in (item.get("publisher"),
                                         (item.get("published") or "")[:16]) if x)
            parts.append(f'<li>{body} <span class="dim">{esc(dim)}</span></li>')
        parts.append("</ul>")
    if discl:
        parts.append("<h4>📄 適時開示（TDnet）</h4><ul>")
        for item in discl:
            title = esc(item["title"])
            body = (f'<a href="{esc(item["url"])}" target="_blank" rel="noopener">{title}</a>'
                    if item.get("url") else title)
            parts.append(f'<li>{body} <span class="dim">{esc((item.get("date") or "")[:10])}</span></li>')
        parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def stock_section(record: dict, prices: dict, badge: str, badge_label: str) -> str:
    code, name = record["code"], record["name"]
    s = record["signal"]
    views = record.get("expert_views") or {}
    levels = {}
    if s.get("target_price"):
        levels["目標"] = float(s["target_price"])
    if s.get("stop_loss"):
        levels["損切り"] = float(s["stop_loss"])
    kl = (views.get("technical") or {}).get("key_levels") or {}
    if kl.get("support"):
        levels.setdefault("支持線", float(kl["support"]))
    if kl.get("resistance"):
        levels.setdefault("抵抗線", float(kl["resistance"]))

    pts = prices.get(code) or []
    chart = build_chart_svg(pts, levels)
    last = pts[-1] if pts else None

    parts = [f'<div class="stock" id="s{esc(code)}">']
    parts.append(f'<h2>{esc(code)} {esc(name)}'
                 f'<span class="badge {badge}">{esc(badge_label)}</span></h2>')
    meta = [f"確信度 {s['confidence']}"]
    if last:
        meta.append(f"終値 {fmt_price(last['close'])}（{last['date']}）")
    parts.append(f'<div class="meta-line">{esc(" ／ ".join(meta))}</div>')
    if chart:
        parts.append(f'<div class="chart">{chart}'
                     '<div class="legend">— 終値　— SMA5（橙）　-- SMA25（紫）　'
                     '破線: 目標（緑）/ 損切り（赤）/ 節目（灰）　下段: 出来高</div></div>')
    parts.append('<div class="cols">')
    parts.append(expert_card("📈 テクニカル専門家", views.get("technical")))
    parts.append(expert_card("📊 ファンダメンタルズ専門家", views.get("fundamental")))
    parts.append(synthesis_card(record))
    parts.append("</div>")
    parts.append(refs_block(record))
    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# ページ全体
# ---------------------------------------------------------------------------

def build_html(date: str, records: list, macro: list, missing_sources=None) -> str:
    prices = load_price_series()
    actionable, holds = split_records(records)
    watch, others = split_holds(holds)

    counts = {"buy": 0, "sell": 0, "hold": 0}
    for r in records:
        counts[r["signal"]["signal"]] += 1

    out = ["<!DOCTYPE html>", '<html lang="ja"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>デイリーシグナルレポート {esc(date)}</title>",
           f"<style>{CSS}</style></head><body>", '<div class="wrap">']
    out.append(f"<h1>📈 デイリーシグナルレポート {esc(date)}</h1>")
    out.append('<div class="sub">テクニカル専門家 × ファンダメンタルズ専門家 × '
               'チーフアナリスト統合（trade-pilot PoC）</div>')
    out.append(f'<div class="counts">買い {counts["buy"]} ／ 売り {counts["sell"]} ／ '
               f'注目 {len(watch)} ／ 様子見 {counts["hold"] - len(watch)}'
               f'（全 {len(records)} 銘柄）</div>')

    if actionable:
        out.append('<h2 class="section-h">🟢 売買シグナル</h2>')
        for r in actionable:
            sig = r["signal"]["signal"]
            out.append(stock_section(r, prices, SIGNAL_CLASS[sig], SIGNAL_JP[sig]))
    else:
        out.append('<h2 class="section-h">🟢 売買シグナル</h2><p>本日の買い / 売りシグナルはありません。</p>')

    if watch:
        out.append('<h2 class="section-h">👀 注目（買い候補・監視中: ファンダ強気 × テクニカル中立）</h2>')
        for r in watch:
            out.append(stock_section(r, prices, "watch", "注目"))

    if others:
        out.append('<h2 class="section-h">⚪ 様子見</h2>')
        for r in others:
            out.append(stock_section(r, prices, "hold", "様子見"))

    out.append('<h2 class="section-h">🌐 マクロスナップショット</h2>')
    if macro:
        out.append('<table class="macro"><tr><th>指標</th><th>値</th><th>前日比</th><th>基準日</th></tr>')
        for m in macro:
            out.append(f"<tr><td>{esc(m['label'])}</td><td>{esc(m['value'])}</td>"
                       f"<td>{esc(m['change'] or '—')}</td><td>{esc(m['as_of'])}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p>マクロデータなし</p>")

    if missing_sources:
        out.append('<div class="missing">⚠ 本日取得に失敗したデータソース（前回取得分で代替）: '
                   + esc("、".join(missing_sources)) + "</div>")

    out.append(f'<div class="footer">生成時刻: '
               f'{datetime.now(JST).isoformat(timespec="seconds")} ／ '
               '本レポートは PoC の自動生成であり投資助言ではありません。</div>')
    out.append("</div></body></html>")
    return "\n".join(out)


def build_index_html(dates: list) -> str:
    """docs/reports/ 配下のレポート一覧ページ。dates は降順の YYYY-MM-DD リスト。"""
    out = ["<!DOCTYPE html>", '<html lang="ja"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>trade-pilot デイリーレポート一覧</title>",
           f"<style>{CSS}</style></head><body>", '<div class="wrap">',
           "<h1>📚 デイリーレポート一覧</h1><ul>"]
    for d in dates:
        out.append(f'<li><a href="{esc(d)}.html">{esc(d)}</a></li>')
    out.append("</ul></div></body></html>")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="PoC-5 リッチ版 HTML レポート構築")
    parser.add_argument("--date", default=today_jst(), help="対象日 YYYY-MM-DD")
    parser.add_argument("--out", default=None, help="出力先（省略時: data/reports/<date>.html）")
    args = parser.parse_args()
    try:
        records = load_signals(args.date)
    except FileNotFoundError as e:
        print(f"[NG] {e}", file=sys.stderr)
        return 1
    html_text = build_html(args.date, records, load_macro_snapshot())
    out = Path(args.out) if args.out else DATA_DIR / "reports" / f"{args.date}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(f"[OK] HTML レポート: {out}（{len(html_text):,} 文字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
