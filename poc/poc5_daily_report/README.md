# PoC-5: デイリーレポート生成・Slack配信

PoC-1（データ取得）と PoC-3（LLM シグナル生成）をつなぎ、
「毎朝寄り付き前に universe 全銘柄のシグナルをまとめたレポートが自動で届く」
パイプラインの試作。

## 構成

| ファイル | 役割 |
|---------|------|
| `run_daily.py` | 毎朝の一括実行（取得 → シグナル生成 → レポート → 配信 → 履歴保存） |
| `report_builder.py` | `data/signals/<date>/*.json` から Markdown / Slack mrkdwn レポートを構築 |
| `html_report.py` | リッチ版 HTML レポート（SVG チャート + 両専門家の見解 + 総合判断 + 引用リンク） |
| `slack_notify.py` | Slack Incoming Webhook への送信（分割対応・未設定時は stdout フォールバック） |
| `signals_history/<date>/` | シグナル JSON の履歴（**git 管理下**。フォワードテスト 4 週間の記録用） |
| `reports_history/<date>.md` | レポート Markdown の履歴（**git 管理下**） |
| `../../docs/reports/<date>.html` | リッチ版 HTML の公開先（**git 管理下**。GitHub Pages 配信用） |
| `../../.github/workflows/daily-report.yml` | GitHub Actions 定義（平日朝 JST 7:00 目安） |

`data/` 配下（`data/reports/<date>.md` 含む）は gitignore 済みのローカル成果物。
git に残す履歴は `signals_history/` と `reports_history/`、HTML は `docs/reports/` にコピーされる。

## パイプラインの流れ（`run_daily.py`）

1. **データ取得**: PoC-1 の fetch スクリプトを順に subprocess 実行
   - `fetch_prices_yfinance` → `fetch_news_google` → `fetch_news_macro_rss` → `fetch_disclosures_yanoshin` → `fetch_macro`
   - **J-Quants と EDINET は朝バッチでは呼ばない**（J-Quants の前営業日データは夕方更新、財務は低頻度のため）
   - ソース単位の失敗はスキップして続行し、レポート末尾に「欠損ソース」として明記
2. **シグナル生成**: PoC-3 `generate_signal.py --provider gemini` を subprocess 実行
3. **レポート構築 → 配信**: `data/reports/<date>.md` に保存 → Slack 送信（URL 未設定なら stdout）
4. **履歴永続化**: `data/signals/<date>/` → `signals_history/<date>/`、レポート md → `reports_history/<date>.md`

## レポートの内容

Slack / Markdown（要約版）:

- サマリー行（日付、buy/sell/hold の件数）+ リッチ版 HTML へのリンク
- 売買シグナル（buy/sell を確信度降順）: 判定・確信度・現在値・目標/損切り・想定保有日数・理由の要約・リスク 1 行
- 👀 注目（買い候補・監視中）: hold のうち「ファンダ強気(strength≥60) × テクニカル中立」の銘柄
  （v5 で buy 発火をファンダ strength≥70 に引き上げたため、60〜69 のカタリスト待ちを可視化）
- 様子見（hold）銘柄は 1 行ずつ
- マクロスナップショット（日経平均・TOPIX ETF・ドル円・VIX・日米金利）
- 欠損ソース（取得失敗があった場合のみ）

Slack は 1 メッセージ 3,000 字を超える場合に自動分割される。

リッチ版 HTML（`html_report.py`、外部依存なしの自己完結 1 ファイル）は銘柄ごとに:

- 株価チャート（直近60営業日の終値 + SMA5/25 + 目標/損切り/節目ライン + 出来高、SVG 手描画）
- テクニカル専門家・ファンダメンタルズ専門家それぞれの見解（stance/strength・根拠 + evidence・注意点・節目）
- チーフアナリストの総合判断（シグナル・確信度・目標/損切り・理由・リスク・再エントリー抑制の状態）
- 参照ニュース・適時開示へのリンク（シグナルレコードの `context_refs`。LLM 入力に含まれた見出しの原典）

### HTML レポートの公開（GitHub Pages、初回のみ手動設定）

リポジトリの **Settings → Pages → Build and deployment** で
Source = `Deploy from a branch`、Branch = `main` / `/docs` を選択すると、
`https://<user>.github.io/trade-pilot/reports/<date>.html` で配信される
（Slack のリンクはこの URL を指す。`REPORT_PAGES_URL` 環境変数で上書き可）。

## ローカル実行

```bash
cd poc/poc5_daily_report

# フル実行（取得 → 生成 → レポート → 配信 → 履歴保存）
../../.venv/bin/python run_daily.py

# 既存の data/signals/<今日>/ からレポート構築 + 送信のみ（履歴保存なし）
../../.venv/bin/python run_daily.py --report-only

# 日付指定
../../.venv/bin/python run_daily.py --report-only --date 2026-07-04

# レポートの表示のみ（送信なし）
../../.venv/bin/python report_builder.py --date 2026-07-04

# Slack 疎通テスト
../../.venv/bin/python slack_notify.py "テストメッセージ"
```

必要な環境変数（リポジトリルートの `.env`）:

| 変数 | 必須 | 用途 |
|------|------|------|
| `GEMINI_API_KEY` | ○ | PoC-3 シグナル生成 |
| `SLACK_WEBHOOK_URL` | △ | Slack 配信（未設定なら stdout に出力） |
| `FRED_API_KEY` | △ | マクロ指標の米金利（未設定なら FRED のみ欠損） |

## GitHub Actions セットアップ

1. リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録:
   - `GEMINI_API_KEY`（必須）
   - `SLACK_WEBHOOK_URL`（Slack の Incoming Webhook URL。App 作成 → Incoming Webhooks 有効化 → チャンネル選択で発行）
   - `FRED_API_KEY`（任意。米金利をレポートに載せる場合）
2. ワークフロー `.github/workflows/daily-report.yml` は push 済みなら自動で有効になる。
   **Actions タブ → daily-report → Run workflow** で手動実行（workflow_dispatch）して動作確認できる。
3. スケジュール: `cron: '0 22 * * 0-4'`（UTC）
   - UTC 22:00 = **JST 翌朝 7:00**。JST の月〜金の朝に配信するため、UTC では日〜木に起動する（JST 月曜朝 = UTC 日曜 22:00）。
4. 実行後、`signals_history/` と `reports_history/` の差分を `GITHUB_TOKEN` で自動 commit & push する（`permissions: contents: write`）。

## 制約・注意事項

- **GitHub Actions の cron は起動遅延がある**（混雑状況により数分〜数十分）。
  JST 7:00 設定でも実際の配信は 7:00〜7:40 程度になりうる。8:30（寄り付き前）厳守の観点では
  余裕を持った設定であり、遅延が常態化する場合は self-hosted runner / 自宅 cron / クラウドスケジューラを検討する。
- レポートが届かないこと自体に気づく仕組み（死活監視）は本 PoC のスコープ外。
  当面は GitHub の workflow 失敗通知メールで代替する。
- Slack 送信失敗時はワークフローが exit 1 で失敗になる（気づけるようにするため）。
  レポート md とシグナル履歴は失敗時もコミットされるので、`--report-only` で再送できる。
- Gemini 無料枠のレート制限（Flash 系 15 req/分）のため、シグナル生成は 1 銘柄 5 秒以上の間隔で
  実行される。11 銘柄で 1〜2 分、50 銘柄で 5〜10 分程度を想定。
- コードは Python 3.9（ローカル `.venv`）/ 3.11（GitHub Actions）の両方で動作する。
