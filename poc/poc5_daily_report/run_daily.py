"""PoC-5: 毎朝の一括実行パイプライン。

流れ:
    a. データ取得   — PoC-1 の fetch スクリプトを順に subprocess 実行
                      （J-Quants と EDINET は朝バッチでは呼ばない。
                        ソース単位の失敗はスキップして続行し、レポート末尾に明記）
    b. シグナル生成 — PoC-3 generate_signal.py --provider gemini を subprocess 実行
    c. レポート     — 構築 → data/reports/<date>.md 保存 → Slack 送信（or stdout）
    d. 履歴永続化   — data/signals/<date>/ と レポート md を git 管理下
                      （signals_history/ と reports_history/）にコピー
                      （PoC-3 フォワードテスト 4 週間の履歴を残すため）

使い方:
    # フル実行（毎朝バッチ・GitHub Actions 用）
    ../../.venv/bin/python run_daily.py

    # 既存の data/signals/<date>/ からレポート構築 + 送信のみ
    ../../.venv/bin/python run_daily.py --report-only [--date YYYY-MM-DD]
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from html_report import build_html, build_index_html
from report_builder import (DATA_DIR, REPO_ROOT, SIGNALS_DIR, build_report,
                            load_macro_snapshot, load_signals, today_jst)
from slack_notify import send_messages

POC_DIR = Path(__file__).resolve().parent
POC1_DIR = REPO_ROOT / "poc" / "poc1_data_sources"
POC3_DIR = REPO_ROOT / "poc" / "poc3_llm_signal_quality"

REPORTS_DIR = DATA_DIR / "reports"                  # gitignore 下（ローカル成果物）
SIGNALS_HISTORY_DIR = POC_DIR / "signals_history"   # git 管理下（フォワードテスト履歴）
REPORTS_HISTORY_DIR = POC_DIR / "reports_history"   # git 管理下
DOCS_REPORTS_DIR = REPO_ROOT / "docs" / "reports"   # git 管理下（GitHub Pages 配信用）

# 朝バッチで実行する PoC-1 fetch スクリプト（実行順）。
# J-Quants（前営業日データは夕方更新）と EDINET（財務は低頻度）は対象外。
# summarize_disclosures は fetch_disclosures_yanoshin の出力に依存するため後に置く
# （新規の重要開示のみ Gemini で要約し、既要約分はキャッシュを使う）。
FETCH_SCRIPTS = [
    "fetch_prices_yfinance.py",
    "fetch_fundamentals_yfinance.py",
    "fetch_news_google.py",
    "fetch_news_macro_rss.py",
    "fetch_disclosures_yanoshin.py",
    "summarize_disclosures.py",
    "fetch_macro.py",
    "fetch_macro_jfc.py",
    "fetch_macro_boj.py",
    "fetch_macro_pmi.py",
    "fetch_macro_cpi.py",
]

FETCH_TIMEOUT_SEC = 600
SIGNAL_TIMEOUT_SEC = 5400  # 50銘柄 × (5s スロットル + リトライ) を許容


def run_script(script_path: Path, args=None, cwd=None, timeout=FETCH_TIMEOUT_SEC) -> bool:
    """スクリプトを subprocess 実行し、成功したかを返す（例外は投げない）。"""
    cmd = [sys.executable, str(script_path)] + list(args or [])
    print(f"\n=== 実行: {' '.join(cmd)} ===")
    try:
        result = subprocess.run(cmd, cwd=str(cwd or script_path.parent), timeout=timeout)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[NG] タイムアウト ({timeout}s): {script_path.name}", file=sys.stderr)
        return False
    except OSError as e:
        print(f"[NG] 実行失敗: {script_path.name}: {e}", file=sys.stderr)
        return False


def step_fetch() -> list:
    """PoC-1 の fetch スクリプトを順に実行し、失敗ソース名のリストを返す。"""
    missing = []
    for script in FETCH_SCRIPTS:
        if not run_script(POC1_DIR / script):
            missing.append(script.replace("fetch_", "").replace(".py", ""))
    return missing


def step_generate_signals() -> bool:
    """PoC-3 のシグナル生成を実行する。"""
    return run_script(
        POC3_DIR / "generate_signal.py",
        args=["--provider", "gemini"],
        timeout=SIGNAL_TIMEOUT_SEC,
    )


def step_report(date: str, missing_sources: list) -> bool:
    """レポート構築 → data/reports/<date>.md 保存 → Slack 送信。"""
    try:
        markdown, slack_messages = build_report(date, missing_sources or None)
    except FileNotFoundError as e:
        print(f"[NG] レポート構築失敗: {e}", file=sys.stderr)
        return False

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{date}.md"
    report_path.write_text(markdown, encoding="utf-8")
    print(f"[OK] レポート保存: {report_path}")

    # リッチ版 HTML（チャート + 両専門家の見解 + 引用リンク）。
    # 失敗しても Slack 送信は続行する（HTML はあくまで詳細ビュー）
    try:
        html_text = build_html(date, load_signals(date), load_macro_snapshot(),
                               missing_sources or None)
        html_path = REPORTS_DIR / f"{date}.html"
        html_path.write_text(html_text, encoding="utf-8")
        print(f"[OK] HTML レポート保存: {html_path}")
    except Exception as e:  # noqa: BLE001 - レポート補助機能のため広く握る
        print(f"[WARN] HTML レポート構築失敗: {type(e).__name__}: {e}", file=sys.stderr)

    sent = send_messages(slack_messages)
    if not sent:
        print("[WARN] Slack 送信に失敗しました（レポート自体は保存済み）。", file=sys.stderr)
    return sent


def step_persist_history(date: str) -> None:
    """シグナルとレポートを git 管理下の履歴ディレクトリにコピーする。"""
    src_signals = SIGNALS_DIR / date
    if src_signals.is_dir():
        dst = SIGNALS_HISTORY_DIR / date
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_signals, dst, dirs_exist_ok=True)
        print(f"[OK] シグナル履歴コピー: {dst}")
    else:
        print(f"[WARN] シグナルディレクトリなし（履歴コピーをスキップ）: {src_signals}",
              file=sys.stderr)

    src_report = REPORTS_DIR / f"{date}.md"
    if src_report.is_file():
        REPORTS_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        dst_report = REPORTS_HISTORY_DIR / f"{date}.md"
        shutil.copy2(src_report, dst_report)
        print(f"[OK] レポート履歴コピー: {dst_report}")

    # HTML は GitHub Pages 配信用の docs/reports/ に置き、一覧 index も更新する
    src_html = REPORTS_DIR / f"{date}.html"
    if src_html.is_file():
        DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_html, DOCS_REPORTS_DIR / f"{date}.html")
        dates = sorted((p.stem for p in DOCS_REPORTS_DIR.glob("????-??-??.html")),
                       reverse=True)
        (DOCS_REPORTS_DIR / "index.html").write_text(
            build_index_html(dates), encoding="utf-8")
        print(f"[OK] HTML レポート公開コピー: {DOCS_REPORTS_DIR / (date + '.html')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="PoC-5 デイリーレポート一括実行")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="データ取得・シグナル生成をスキップし、既存シグナルからレポート構築+送信のみ",
    )
    parser.add_argument(
        "--date",
        default=today_jst(),
        help="対象日 YYYY-MM-DD（省略時: 今日 JST）",
    )
    args = parser.parse_args()

    missing_sources = []
    if not args.report_only:
        # a. データ取得（失敗はスキップして続行）
        missing_sources = step_fetch()
        if missing_sources:
            print(f"\n[WARN] 取得失敗ソース: {missing_sources}（続行します）", file=sys.stderr)

        # b. シグナル生成（一部銘柄の失敗は generate_signal.py 側で許容される）
        if not step_generate_signals():
            print("[WARN] シグナル生成が非ゼロ終了（生成済み分でレポートを試みます）",
                  file=sys.stderr)

    # c. レポート構築 → 保存 → 送信
    report_ok = step_report(args.date, missing_sources)
    if not report_ok and not (SIGNALS_DIR / args.date).is_dir():
        return 1  # シグナルが 1 件もない = レポート不能

    # d. 履歴永続化（--report-only では git 管理下を変更しない）
    if not args.report_only:
        step_persist_history(args.date)

    return 0 if report_ok else 1


if __name__ == "__main__":
    sys.exit(main())
