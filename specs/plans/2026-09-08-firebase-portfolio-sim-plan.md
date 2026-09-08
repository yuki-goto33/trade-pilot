# Firebase公開 + 売買シミュレーション 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** シグナル完全遵従の売買シミュレーション（毎日フル再計算）を実装し、資産推移ページを Firebase Hosting（無料）で公開する。

**Architecture:** `simulate.py`（stateless replay エンジン）→ `portfolio_page.py`（docs/portfolio.json + portfolio.html 生成）→ `run_daily.py` 統合 → 既存コミットステップ → Firebase deploy workflow（push の docs/** 変更で発火）。

**Tech Stack:** Python 3.11（CI）/ 3.9（ローカル .venv）、yfinance、pytest、インライン SVG、Firebase Hosting Spark プラン。

## Global Constraints

- すべて無料: Firebase Spark プラン維持（Blaze 禁止）、yfinance、public repo Actions
- 外部 JS/CSS ライブラリ不使用（既存 html_report.py と同じ自己完結 HTML）
- 設計書: `specs/2026-09-08-firebase-portfolio-sim-design.md`（ルール詳細はこちらが正）
- コミットは Conventional Commits 形式・日本語 subject（既存慣習）

---

### Task 1: simulate.py — シミュレーションエンジン（TDD）

**Files:**
- Create: `poc/poc5_daily_report/simulate.py`
- Test: `poc/poc5_daily_report/test_simulate.py`

**Interfaces (Produces):**
- `load_signal_history(history_dir: Path) -> dict[str, list[dict]]` — date → [{code, name, signal, confidence, target_price, stop_loss, holding_period_days}]（signals_history/<date>/<code>.json を平坦化。スキーマ不整合はスキップ）
- `fetch_ohlc(codes: list[str], start: str) -> dict[str, pd.DataFrame]` — code → OHLC 日足 DataFrame（yf.download auto_adjust=False, splits で株数調整用に 'Stock Splits' 相当も保持）
- `run_simulation(signals_by_date, ohlc_by_code, initial_cash=3_000_000, allocation=0.10) -> SimulationResult`
- `SimulationResult`: dataclass — `daily: list[{date, equity, cash, n_positions}]`, `trades: list[{code, name, entry_date, entry_price, exit_date, exit_price, shares, pnl, pnl_pct, exit_reason}]`, `positions: list[{code, name, entry_date, entry_price, shares, last_price, value, unrealized_pnl_pct}]`

**ルール（設計書の通り）:** buy=当日始値・現金10%・端株可 / exit 優先: stop(安値≤stop→min(始値,stop)) > target(高値≥target→max(始値,target)) > holding_period_days(暦日)経過→翌営業日始値 > sell シグナル→当日始値 / 保有中 buy 無視 / null target・stop は期間のみ / 分割は株数調整 / 価格欠損日は前日値持ち越し

**Steps:**
- [ ] テスト作成（合成データ）: entry 約定 / stop 約定・ギャップ / target 約定 / 同日両タッチ stop 優先 / 期間満了売り / 保有中 buy 無視 / null target・stop / 価格欠損持ち越し / 資金10%配分
- [ ] `pytest poc/poc5_daily_report/test_simulate.py -v` で FAIL 確認
- [ ] 実装 → PASS 確認
- [ ] コミット `feat(poc5): 売買シミュレーションエンジン`

### Task 2: portfolio_page.py — 資産推移ページ生成

**Files:**
- Create: `poc/poc5_daily_report/portfolio_page.py`
- Test: `poc/poc5_daily_report/test_portfolio_page.py`（スモーク: 合成 SimulationResult → HTML/JSON に主要素が含まれる）

**Interfaces:**
- Consumes: `SimulationResult`（Task 1）
- Produces: `build_portfolio_json(result, benchmark: list) -> dict` / `build_portfolio_html(result, benchmark: list) -> str` / `main()` — signals_history から一式生成して `docs/portfolio.json` `docs/portfolio.html` へ書き出し（単体実行可能）
- benchmark: TOPIX 連動 ETF 1306.T に初期資金全額投資した推移 `[{date, equity}]`

**内容:** 資産推移チャート（インライン SVG、simulation vs benchmark 2 本線）/ サマリー（現在資産・損益率・勝率・取引数）/ 現在ポジション表 / 取引履歴表 / 前提の明記（手数料ゼロ・端株可・シグナル完全遵従・シミュレーションであり投資助言でない）

**Steps:**
- [ ] スモークテスト作成 → FAIL → 実装 → PASS
- [ ] コミット `feat(poc5): 資産推移ページ生成（portfolio.html/json）`

### Task 3: run_daily.py 統合 + レポート一覧リンク

**Files:**
- Modify: `poc/poc5_daily_report/run_daily.py`（step_report 後に simulate ステップ。失敗しても続行）
- Modify: `poc/poc5_daily_report/html_report.py` の `build_index_html`（資産推移ページへのリンク追加）

**Steps:**
- [ ] run_daily.py に `step_portfolio()` 追加（portfolio_page.main を subprocess or 直接呼び出し、例外は WARN で握る）
- [ ] index にリンク追加
- [ ] 既存テストなし → `python run_daily.py --report-only` 相当のローカル動作確認
- [ ] コミット `feat(poc5): 朝バッチに資産シミュレーション統合`

### Task 4: バックフィル実行・検証

**Steps:**
- [ ] ローカルで `portfolio_page.py` 実行 → 2026-07-04 からの資産推移生成
- [ ] 取引履歴を目視確認（buy シグナル日と約定・exit の整合）
- [ ] `docs/portfolio.html` をブラウザ確認
- [ ] コミット `feat(poc5): 資産推移バックフィル（2026-07-04〜）`

### Task 5: Firebase Hosting セットアップ

**REQUIRED SUB-SKILL:** `setup-firebase-hosting`（Spark プラン・課金なしを明示確認）

**Files:**
- Create: `firebase.json`（public: docs）, `.firebaserc`
- Create: `.github/workflows/firebase-deploy.yml`（on: push main, paths: docs/**）

**Steps:**
- [ ] スキル実行（プロジェクト作成 → SA → Secret 登録 → 設定ファイル → workflow）
- [ ] Spark プランのままであることを確認（Blaze 化しない）
- [ ] コミット & push → deploy workflow 成功確認 → 公開 URL 確認

### Task 6: 最終検証

- [ ] pytest 全通過
- [ ] push 後の firebase-deploy workflow 成功・公開 URL で portfolio.html 表示確認
- [ ] 翌朝の daily-report 実行で portfolio が自動更新されることの確認方法を README/レポートに残す
