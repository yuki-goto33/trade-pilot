# 設計: Firebase Hosting 公開 + 売買シミュレーション（資産推移ページ）

日付: 2026-09-08
ステータス: 承認済み

## 背景と目的

trade-pilot は毎朝 GitHub Actions でシグナル生成とデイリーレポート配信（Slack + GitHub Pages）を行っている。
本設計は次の 2 つを追加する。

1. **Firebase Hosting での公開** — レポートサイトを Firebase Hosting（Spark プラン・無料）で配信する。GitHub Pages は当面併存。
2. **売買シミュレーション** — シグナルに完全遵従して売買した場合の資産推移を毎日計算し、同じサイトで公開する。

制約: **すべて無料で運用する**（Firebase Spark プラン / yfinance / public リポジトリの GitHub Actions / Gemini 無料枠）。

## 全体アーキテクチャ

- `poc/poc5_daily_report/simulate.py` — シミュレーションエンジン（毎日フル再計算 / stateless replay）
- `poc/poc5_daily_report/portfolio_page.py` — 資産推移ページ（`docs/portfolio.html`）と機械可読データ（`docs/portfolio.json`）の生成
- `run_daily.py` — レポート構築後に simulate ステップを追加。失敗しても Slack 送信・コミットは続行
- 既存のコミットステップ（`git add docs`）が生成物をそのまま拾う
- Firebase Hosting — `docs/` を公開ディレクトリとし、main への push（paths: `docs/**`）で自動デプロイする独立 workflow
- レポート一覧 `docs/reports/index.html` に資産推移ページへのリンクを追加

### 毎日フル再計算を選んだ理由（案 A）

状態ファイルを持たず、`signals_history/`（2026-07-04 から蓄積、43 日分）と yfinance の日足全期間を入力に、
毎日ゼロから決定論的にリプレイする。ルール変更・バグ修正が過去に遡って一貫し、実行スキップ日があっても翌日自動復旧する。
データ量は 34 銘柄 × 数ヶ月の日足で、計算コストは数秒。増分更新（案 B）は状態不整合リスクに見合わない。

## シミュレーションルール

- 初期資金: 300 万円。開始日: signals_history の最古日（2026-07-04）
- エントリー: buy シグナル当日の**始値**で購入。投入額は現金残高の 10%。端株可（株数 = 投入額 / 始値、小数可）
- シグナルは前日終値までのデータで生成されるため、当日始値約定に先読みバイアスはない
- イグジット優先順位（日足 OHLC で判定）:
  1. stop_loss: 安値 ≤ stop → 約定価格 `min(始値, stop)`（ギャップダウン対応）
  2. target_price: 高値 ≥ target → 約定価格 `max(始値, target)`（ギャップアップ対応）
  3. 同日に両方タッチ → 保守的に stop 優先
  4. holding_period_days（暦日）経過 → 翌営業日の始値で売却
  5. sell シグナル → 当日始値で売却
- 保有中の同銘柄への buy 再シグナルは無視（追加買いなし）。hold / watch は取引なし
- target_price / stop_loss が null の buy は holding_period_days のみで出口
- 株式分割: yfinance の分割情報で保有株数のみ調整（約定判定は未調整の生値 `auto_adjust=False` を使用）
- 手数料・スリッページ・税金: ゼロ（ページに前提として明記）

## データフロー

```
signals_history/<date>/<code>.json（全日付・全銘柄）
        │
        ▼
simulate.py ── yfinance で全銘柄の日足 OHLC を一括取得（1 リクエスト・auto_adjust=False）
        │        日次リプレイ: エントリー/イグジット判定 → 現金・保有・評価額を日ごとに記録
        ▼
SimulationResult（日次資産推移・全取引履歴・現在ポジション）
        │
        ├─▶ docs/portfolio.json（機械可読）
        └─▶ docs/portfolio.html（資産推移チャート・TOPIX 比較・取引履歴・現在ポジション・前提の明記）
        │
        ▼
既存コミットステップ（git add docs）→ main push → Firebase deploy workflow
```

- TOPIX 比較線: 同期間に初期資金を TOPIX（^TOPX、取得不可なら 1306.T で代替）に全額投資した場合の推移
- HTML チャートは既存 `html_report.py` と同様に外部ライブラリ不使用（インライン SVG）で自己完結

## Firebase Hosting

- `setup-firebase-hosting` スキルで構築: プロジェクト作成 → Spark プラン確認 → サービスアカウント発行 →
  GitHub Secret 登録 → `firebase.json`（public: `docs/`）→ デプロイ workflow（`.github/workflows/firebase-deploy.yml`）
- トリガー: main への push で `docs/**` に変更がある場合のみ
- **無料条件の確認**: Spark プランのまま（Blaze にアップグレードしない）。Hosting 無料枠は
  ストレージ 10GB・転送 360MB/日で、静的 HTML 数十ファイルの本用途では到達しない

## エラー処理

- simulate ステップが失敗しても Slack 送信・履歴コミットは続行（既存パターン踏襲）。レポート末尾に失敗を明記
- yfinance で価格が取得できない銘柄・日: 評価額は前日値を持ち越し、portfolio.html に欠損として明記
- シグナル JSON のスキーマ不整合（欠損キー等）: その銘柄・日をスキップして続行

## テスト

- pytest 単体テスト（`poc/poc5_daily_report/test_simulate.py`）: 合成価格・合成シグナルで
  エントリー・各イグジット経路・stop 優先・資金配分・分割調整・価格欠損時の持ち越しを検証
- 実データ検証: 2026-07-04 からのバックフィル結果の取引履歴を目視確認してからコミット

## スコープ外

- GitHub Pages の無効化(Firebase 安定後に判断)
- 複数ポートフォリオ戦略の比較、パラメータ最適化
- 独自ドメイン
