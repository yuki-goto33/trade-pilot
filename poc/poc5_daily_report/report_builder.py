"""PoC-5: data/signals/<date>/ の LLM シグナルから日次レポートを構築する。

出力は 2 形式:
- Markdown 文字列（data/reports/<date>.md への保存用）
- Slack 向け mrkdwn テキストのリスト
  （Slack の 1 メッセージ制限を考慮し 3,000 字を超える場合は分割）

単体でも実行できる:
    ../../.venv/bin/python report_builder.py [--date YYYY-MM-DD]
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

POC_DIR = Path(__file__).resolve().parent
REPO_ROOT = POC_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data"
SIGNALS_DIR = DATA_DIR / "signals"

# リッチ版 HTML レポートの公開 URL（GitHub Pages: Settings → Pages →
# main ブランチ /docs を有効化すると配信される）。環境変数で上書き可。
REPORT_PAGES_URL = os.environ.get(
    "REPORT_PAGES_URL", "https://yuki-goto33.github.io/trade-pilot/reports")

JST = timezone(timedelta(hours=9))

# Slack の 1 メッセージあたり推奨上限（text は 40,000 字まで受けるが、
# 表示が折りたたまれない実用上の目安として 3,000 字で分割する）
SLACK_MESSAGE_LIMIT = 3000

SIGNAL_LABEL = {"buy": "買い", "sell": "売り", "hold": "様子見"}
SIGNAL_MARK = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}

# マクロスナップショットに載せる yfinance 系列（macro_yfinance.csv の ticker 列）
MACRO_TICKERS = [
    ("^N225", "日経平均"),
    ("1306.T", "TOPIX連動ETF"),
    ("JPY=X", "ドル円"),
    ("^VIX", "VIX"),
]


def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# データ読み込み
# ---------------------------------------------------------------------------

def load_signals(date: str) -> list:
    """data/signals/<date>/*.json を読み込んで返す。無ければ FileNotFoundError。"""
    sig_dir = SIGNALS_DIR / date
    if not sig_dir.is_dir():
        raise FileNotFoundError(f"シグナルディレクトリがありません: {sig_dir}")
    records = []
    for path in sorted(sig_dir.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                records.append(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] シグナル読み込み失敗: {path}: {e}", file=sys.stderr)
    if not records:
        raise FileNotFoundError(f"シグナル JSON が 1 件もありません: {sig_dir}")
    return records


def load_latest_prices() -> dict:
    """data/prices_yfinance.csv から銘柄コード -> 最新終値の辞書を作る。"""
    path = DATA_DIR / "prices_yfinance.csv"
    prices = {}
    if not path.is_file():
        return prices
    per_ticker = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            per_ticker.setdefault(row["ticker"], []).append(row)
    for ticker, rows in per_ticker.items():
        rows.sort(key=lambda r: r["Date"])
        code = ticker.split(".")[0]
        try:
            prices[code] = {"date": rows[-1]["Date"], "close": float(rows[-1]["Close"])}
        except (ValueError, KeyError):
            continue
    return prices


def load_macro_snapshot() -> list:
    """マクロ指標のスナップショット行のリストを返す。

    各行: {"label": str, "value": str, "change": str, "as_of": str}
    データが欠けている指標はスキップする（存在するものだけ返す）。
    """
    rows = []

    # yfinance 系列（日経・TOPIX ETF・ドル円・VIX）
    path = DATA_DIR / "macro_yfinance.csv"
    if path.is_file():
        per_ticker = {}
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                per_ticker.setdefault(row["ticker"], []).append(row)
        for ticker, label in MACRO_TICKERS:
            series = sorted(per_ticker.get(ticker, []), key=lambda r: r["Date"])
            if not series:
                continue
            try:
                last = float(series[-1]["Close"])
                prev = float(series[-2]["Close"]) if len(series) >= 2 else None
            except (ValueError, KeyError):
                continue
            change = ""
            if prev:
                pct = (last - prev) / prev * 100
                change = f"{last - prev:+,.2f} ({pct:+.2f}%)"
            rows.append({
                "label": label,
                "value": f"{last:,.2f}",
                "change": change,
                "as_of": series[-1]["Date"],
            })

    # 日本国債10年（財務省）
    jgb_path = DATA_DIR / "macro_jgb.json"
    if jgb_path.is_file():
        try:
            with open(jgb_path, encoding="utf-8") as f:
                jgb = json.load(f)
            rows.append({
                "label": "日本10年債利回り",
                "value": f"{jgb['jgb_10y_percent']}%",
                "change": "",
                "as_of": jgb.get("date", ""),
            })
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[WARN] macro_jgb.json 読み込み失敗: {e}", file=sys.stderr)

    # FRED（米10年債・FF金利）
    fred_path = DATA_DIR / "macro_fred.json"
    if fred_path.is_file():
        try:
            with open(fred_path, encoding="utf-8") as f:
                fred = json.load(f)
            for series_id in ("DGS10", "DFF"):
                series = fred.get("series", {}).get(series_id)
                if not series:
                    continue
                obs = [o for o in series["observations"] if o["value"] != "."]
                if not obs:
                    continue
                rows.append({
                    "label": series.get("label", series_id),
                    "value": f"{obs[0]['value']}%",
                    "change": "",
                    "as_of": obs[0]["date"],
                })
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[WARN] macro_fred.json 読み込み失敗: {e}", file=sys.stderr)

    return rows


# ---------------------------------------------------------------------------
# 整形ヘルパー
# ---------------------------------------------------------------------------

STANCE_JP = {"bullish": "強気", "bearish": "弱気", "neutral": "中立"}


def expert_stance_str(record: dict) -> str:
    """専門家見解の1行表記（例: "T:強気(75) / F:中立(50)"）。無ければ空文字。"""
    views = record.get("expert_views") or {}
    t, f = views.get("technical"), views.get("fundamental")
    if not (t and f):
        return ""
    return (f"T:{STANCE_JP.get(t['stance'], t['stance'])}({t['strength']}) / "
            f"F:{STANCE_JP.get(f['stance'], f['stance'])}({f['strength']})")


def summarize(text: str, limit: int = 90) -> str:
    """理由テキストを 1 行に要約する（最初の文 + 文字数上限）。"""
    text = " ".join(text.split())  # 改行・連続空白を潰す
    first = text.split("。")[0]
    if first and first != text:
        first += "。"
    if len(first) > limit:
        first = first[: limit - 1] + "…"
    return first


def fmt_price(value) -> str:
    if value is None:
        return "—"
    if float(value) == int(value):
        return f"{int(value):,}円"
    return f"{float(value):,.1f}円"


def split_records(records: list):
    """シグナルを (売買=buy/sell 確信度降順, 様子見=hold 確信度降順) に分ける。"""
    actionable = [r for r in records if r["signal"]["signal"] in ("buy", "sell")]
    holds = [r for r in records if r["signal"]["signal"] == "hold"]
    actionable.sort(key=lambda r: r["signal"]["confidence"], reverse=True)
    holds.sort(key=lambda r: r["signal"]["confidence"], reverse=True)
    return actionable, holds


def is_watch(record: dict) -> bool:
    """「注目（買い候補・監視中）」判定: hold かつ F強気(60+) × T中立。

    v5 で buy 発火をファンダ strength≥70 に引き上げたため、60〜69 の
    bullish（= カタリスト待ち）をレポート上の「注目」として可視化する。
    """
    if record["signal"]["signal"] != "hold":
        return False
    views = record.get("expert_views") or {}
    t = views.get("technical") or {}
    f = views.get("fundamental") or {}
    return (f.get("stance") == "bullish" and (f.get("strength") or 0) >= 60
            and t.get("stance") == "neutral")


def split_holds(holds: list):
    """hold を (注目, その他様子見) に分ける（ともに確信度降順を維持）。"""
    watch = [r for r in holds if is_watch(r)]
    others = [r for r in holds if not is_watch(r)]
    return watch, others


def count_line(records: list) -> str:
    counts = {"buy": 0, "sell": 0, "hold": 0}
    for r in records:
        counts[r["signal"]["signal"]] += 1
    return (f"買い {counts['buy']} / 売り {counts['sell']} / "
            f"様子見 {counts['hold']}（全 {len(records)} 銘柄）")


def current_price_str(record: dict, prices: dict) -> str:
    p = prices.get(record["code"])
    if not p:
        return "—"
    return f"{fmt_price(p['close'])} ({p['date']})"


# ---------------------------------------------------------------------------
# Markdown レポート
# ---------------------------------------------------------------------------

def build_markdown(date: str, records: list, macro: list, missing_sources=None) -> str:
    prices = load_latest_prices()
    actionable, holds = split_records(records)
    watch, holds = split_holds(holds)
    lines = []
    lines.append(f"# デイリーシグナルレポート {date}")
    lines.append("")
    lines.append(f"**{count_line(records)}**")
    lines.append("")

    lines.append("## 売買シグナル（買い / 売り）")
    lines.append("")
    if not actionable:
        lines.append("本日の買い / 売りシグナルはありません。")
        lines.append("")
    for r in actionable:
        s = r["signal"]
        mark = SIGNAL_MARK[s["signal"]]
        label = SIGNAL_LABEL[s["signal"]]
        lines.append(f"### {mark} {label} {r['code']} {r['name']}（確信度 {s['confidence']}）")
        lines.append("")
        stance = expert_stance_str(r)
        if stance:
            lines.append(f"- 専門家見解: {stance}")
        lines.append(
            f"- 現在値: {current_price_str(r, prices)} / "
            f"目標: {fmt_price(s['target_price'])} / "
            f"損切り: {fmt_price(s['stop_loss'])} / "
            f"想定保有: {s['holding_period_days']}日"
        )
        lines.append("- 理由:")
        for reason in s["reasons"]:
            lines.append(f"  - {summarize(reason['reason'])}")
        if s["risks"]:
            lines.append(f"- リスク: {summarize(s['risks'][0])}")
        lines.append("")

    if watch:
        lines.append("## 👀 注目（買い候補・監視中: ファンダ強気 × テクニカル中立）")
        lines.append("")
        for r in watch:
            s = r["signal"]
            first_reason = summarize(s["reasons"][0]["reason"]) if s["reasons"] else "—"
            stance = expert_stance_str(r)
            stance_part = f"［{stance}］" if stance else ""
            lines.append(f"- {r['code']} {r['name']}（{s['confidence']}）{stance_part}: {first_reason}")
        lines.append("")

    lines.append("## 様子見（hold）")
    lines.append("")
    if not holds:
        lines.append("なし")
    for r in holds:
        s = r["signal"]
        first_reason = summarize(s["reasons"][0]["reason"]) if s["reasons"] else "—"
        stance = expert_stance_str(r)
        stance_part = f"［{stance}］" if stance else ""
        lines.append(f"- {r['code']} {r['name']}（{s['confidence']}）{stance_part}: {first_reason}")
    lines.append("")

    lines.append("## マクロスナップショット")
    lines.append("")
    if macro:
        lines.append("| 指標 | 値 | 前日比 | 基準日 |")
        lines.append("|------|-----|--------|--------|")
        for m in macro:
            lines.append(f"| {m['label']} | {m['value']} | {m['change'] or '—'} | {m['as_of']} |")
    else:
        lines.append("マクロデータなし")
    lines.append("")

    if missing_sources:
        lines.append("## 欠損ソース")
        lines.append("")
        lines.append("本日の実行で以下のデータソースの取得に失敗しました（前回取得分で代替）:")
        lines.append("")
        for src in missing_sources:
            lines.append(f"- {src}")
        lines.append("")

    lines.append("---")
    lines.append(f"生成時刻: {datetime.now(JST).isoformat(timespec='seconds')}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack（mrkdwn）レポート
# ---------------------------------------------------------------------------

def build_slack_blocks(date: str, records: list, macro: list, missing_sources=None) -> list:
    """Slack mrkdwn の「段落」リストを返す（分割はこの単位で行う）。"""
    prices = load_latest_prices()
    actionable, holds = split_records(records)
    watch, holds = split_holds(holds)
    blocks = []

    header = f":newspaper: *デイリーシグナルレポート {date}*\n{count_line(records)}"
    if REPORT_PAGES_URL:
        header += (f"\n:bar_chart: <{REPORT_PAGES_URL.rstrip('/')}/{date}.html"
                   "|詳細レポート（チャート・両専門家の見解・引用記事）>")
    blocks.append(header)

    if actionable:
        for r in actionable:
            s = r["signal"]
            mark = SIGNAL_MARK[s["signal"]]
            label = SIGNAL_LABEL[s["signal"]]
            lines = [f"{mark} *{label} {r['code']} {r['name']}*（確信度 {s['confidence']}）"]
            stance = expert_stance_str(r)
            if stance:
                lines.append(f"_専門家見解: {stance}_")
            lines.append(
                f"現在値 {current_price_str(r, prices)} / 目標 {fmt_price(s['target_price'])}"
                f" / 損切り {fmt_price(s['stop_loss'])} / 想定保有 {s['holding_period_days']}日"
            )
            for reason in s["reasons"]:
                lines.append(f"• {summarize(reason['reason'])}")
            if s["risks"]:
                lines.append(f"⚠ {summarize(s['risks'][0])}")
            blocks.append("\n".join(lines))
    else:
        blocks.append("本日の買い / 売りシグナルはありません。")

    if watch:
        lines = ["*👀 注目（買い候補・監視中: ファンダ強気 × テクニカル中立）*"]
        for r in watch:
            s = r["signal"]
            first_reason = summarize(s["reasons"][0]["reason"], limit=70) if s["reasons"] else "—"
            stance = expert_stance_str(r)
            stance_part = f"［{stance}］" if stance else ""
            lines.append(f"• {r['code']} {r['name']}（{s['confidence']}）{stance_part}: {first_reason}")
        blocks.append("\n".join(lines))

    if holds:
        lines = ["*様子見（hold）*"]
        for r in holds:
            s = r["signal"]
            first_reason = summarize(s["reasons"][0]["reason"], limit=70) if s["reasons"] else "—"
            stance = expert_stance_str(r)
            stance_part = f"［{stance}］" if stance else ""
            lines.append(f"• {r['code']} {r['name']}（{s['confidence']}）{stance_part}: {first_reason}")
        blocks.append("\n".join(lines))

    if macro:
        lines = ["*マクロスナップショット*"]
        for m in macro:
            item = f"• {m['label']}: {m['value']}"
            if m["change"]:
                item += f" 前日比 {m['change']}"
            lines.append(item)
        blocks.append("\n".join(lines))

    if missing_sources:
        lines = ["*欠損ソース*（取得失敗、前回取得分で代替）"]
        for src in missing_sources:
            lines.append(f"• {src}")
        blocks.append("\n".join(lines))

    return blocks


def pack_slack_messages(blocks: list, limit: int = SLACK_MESSAGE_LIMIT) -> list:
    """段落リストを、各メッセージが limit 字以内になるよう詰めて返す。"""
    messages = []
    current = ""
    for block in blocks:
        if len(block) > limit:  # 単独で超える段落は強制分割
            if current:
                messages.append(current)
                current = ""
            for i in range(0, len(block), limit):
                messages.append(block[i: i + limit])
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def build_report(date: str, missing_sources=None):
    """レポートを構築して (markdown, slack_messages) を返す。"""
    records = load_signals(date)
    macro = load_macro_snapshot()
    markdown = build_markdown(date, records, macro, missing_sources)
    slack_messages = pack_slack_messages(
        build_slack_blocks(date, records, macro, missing_sources)
    )
    return markdown, slack_messages


def main() -> int:
    parser = argparse.ArgumentParser(description="PoC-5 日次レポート構築（表示のみ）")
    parser.add_argument("--date", default=today_jst(), help="対象日 YYYY-MM-DD（省略時: 今日 JST）")
    args = parser.parse_args()
    try:
        markdown, slack_messages = build_report(args.date)
    except FileNotFoundError as e:
        print(f"[NG] {e}", file=sys.stderr)
        return 1
    print(markdown)
    print("=" * 72)
    print(f"Slack メッセージ数: {len(slack_messages)} "
          f"(文字数: {[len(m) for m in slack_messages]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
