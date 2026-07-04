# ヒストリカルシミュレーション基盤（テクニカル × ファンダ）

過去の各営業日に「その日の朝に入手可能だった情報のみ」で LLM シグナルを生成し、
シグナルに従った売買の成績（勝率・損益・TOPIX 比較・方向的中率）を測る。
既存の PoC-3（プロンプト/スキーマ/LLM クライアント）と PoC-4（バックテスト）を再利用する。

## 構成

| ファイル | 役割 |
|---|---|
| `fetch_news_range.py` | Google News RSS の日付指定検索（`after:D-3 before:D`）で過去見出しを `data/news_history/<code>/<D>.json` にキャッシュ。3秒間隔 + バックオフ。キャッシュ済みはスキップ |
| `historical_context.py` | `build_context_asof(code, date)`: as-of 時点のコンテキストを既存 `build_context` と同一構造で組み立てる。`--fetch` で `data/prices_history.csv` / `data/macro_history.csv`（yfinance 日足）を作成 |
| `run_historical.py` | 営業日 × 銘柄でシグナル生成（`data/signals_historical/<date>/<code>.json`）。生成済みスキップ（resume）・`--max-calls` チャンク実行・`progress.json` 出力 |
| `evaluate_historical.py` | signals.csv 変換 → poc4 バックテスト（手数料0.1%・100株単元・1銘柄1,000万円）→ TOPIX(1306.T) 比較 → 方向的中率（55%基準）→ `data/historical_eval_report.md` |

## as-of 再構成のルール（look-ahead 防止）

- **テクニカル**: date 前営業日までの 30 営業日窓（date 当日の足は含めない）
- **ニュース**: date-3〜date-1 の見出し（Google News の `before:` は当日分が混入するため
  published < date でフィルタ）最大10件
- **開示**: yanoshin 履歴から date 以前 90 日・最大10件のタイトル
- **ファンダ**: 公表時期が date 以前と推定される期のみ（yanoshin の決算短信開示日を優先、
  無ければ期末+45日で近似）。PER/PBR 等は as-of 株価 × 直近公表 EPS / 推定 BPS の近似値。
  次回決算日は yanoshin 履歴の date 直後の決算短信日付から逆引き。開示 PDF 要約は
  過去分が入手不可のため含めない（note に明記）
- **マクロ**: date 前営業日までの 5 営業日窓。過去時点の金利は未取得（note に明記）

## 実行手順（例: 2026年4月）

```bash
# 1. 価格キャッシュ（テクニカル40営業日分の余裕 + 評価用に期間後も含める）
../../../.venv/bin/python historical_context.py --fetch --start 2026-01-20 --end 2026-07-04

# 2. ニュースキャッシュ（チャンク実行可・再実行安全）
../../../.venv/bin/python fetch_news_range.py --start 2026-04-01 --end 2026-04-30

# 3. シグナル生成（resume 対応。--max-calls でチャンク化、--models でモデル指定）
../../../.venv/bin/python run_historical.py --start 2026-04-01 --end 2026-04-30 --max-calls 60

# 4. 評価
../../../.venv/bin/python evaluate_historical.py
```

## 長期間バッチの運用手順（2026-05〜06 の 440 件で検証済み）

1ヶ月を超えるバッチは「ニュース取得を先行させ、生成ループを追従させる」のが最速。

```bash
# 1. ニュース取得をバックグラウンドで開始（473 リクエスト ≒ 29 分。日付昇順に進む）
nohup ../../../.venv/bin/python fetch_news_range.py \
    --start 2026-05-01 --end 2026-06-30 > /tmp/fetch_news.log 2>&1 &

# 2. 生成ループ（resume 前提の再実行ループ）。フェッチと並行して回してよい。
#    ニュース未取得の日付は ContextBuildError で NG になるが LLM を消費せず、
#    次の周回で resume されるため安全。
while true; do
  ../../../.venv/bin/python run_historical.py --start 2026-05-01 --end 2026-06-30
  rem=$(python3 -c "import json;print(json.load(open('../../../data/signals_historical/progress.json'))['remaining'])")
  [ "$rem" -eq 0 ] && break
  sleep 60
done

# 3. 評価（signals_historical 配下の全日付を自動で拾う）
../../../.venv/bin/python evaluate_historical.py
```

- **resume**: `data/signals_historical/<date>/<code>.json` が存在するタスクはスキップ
  されるので、中断・kill・クォータ枯渇のどこで止まっても同じコマンドで再開できる。
  進捗は `data/signals_historical/progress.json`（done_total / remaining / last）で確認
- **チャンク実行**: フォアグラウンドで回す場合は `--max-calls 60` 程度で 10 分以内に
  収まる（順調時 7〜8 秒/件）。上記のループならバックグラウンドで放置できる
- **クォータ対策**: 無料枠はモデル別のトークンバケット（バースト後は 1 分弱の
  cooldown を繰り返す低速リフィル）に加えて日次上限がある。6 モデルローテーションで
  約 290 呼び出し/セッションを消化した後は数件/10分まで失速した。日次クォータは
  **太平洋時間 0 時（JST 16:00）にリセット**され、その後は 7〜20 秒/件に回復する。
  枯渇したらプロセスは生かしたまま待つ（RotatingGeminiClient が cooldown 明けに
  自動再開する）か、一旦止めて JST 16:00 以降に再実行する
- 実測（2026-05-01〜06-30、440 件）: 総 LLM 呼び出し 483 回（スキーマ違反の
  再サンプル込み）、総所要 約 95 分（うちクォータ枯渇による失速 約 30 分）

## 実行結果メモ（2026-04-01〜06-30 フル期間）

- 生成: 61 営業日 × 11 銘柄 = 671 件（4月 231 / 5月 198 / 6月 242）
- 使用モデル内訳: gemini-3.1-flash-lite 572 / gemini-3-flash-preview 47 /
  gemini-2.5-flash 35 / gemini-2.5-flash-lite 9 / その他 8
- 方向的中率 43.1%（n=137、基準 55% 未達。buy 45.6% n=125 / sell 16.7% n=12）。
  月別: 4月 50.0% (n=30) → 5月 47.2% (n=53) → 6月 35.2% (n=54)
- バックテスト: +24,198,019 円（+22.0%）、対 TOPIX +11.08pt、最大DD -12.29%。
  ただし 285A・9984 の2銘柄が利益の大半で、銘柄別では 11 銘柄中 4 銘柄が B&H 負け
- 詳細は `data/historical_eval_report.md`（gitignore 対象・ローカルのみ）

## 評価の意味論

- シグナルは「as-of 日 D の朝に D-1 引けまでのデータで生成」→ poc4 の
  「date 行 = その日の引けの判断 → 翌営業日寄り付き約定」に合わせ、
  signals.csv の date には price_as_of（D の前営業日）を入れる（約定は D の寄り付き）
- 方向的中率: holding_period_days（暦日）以内に target 到達=的中 / stop 到達=外れ /
  期限切れは含み損益の方向。基準 55%
- バックテストは sell シグナルが出るまで保有し続ける（holding_period_days は使わない）。
  評価期間末に強制決済

## 制約・注意

- Gemini 無料枠はモデルごとに小さなトークンバケット型クォータ（バースト 20 リクエスト
  規模 + 低速リフィル）。単一モデルでは 200 件超のバッチが完走できないため、
  `run_historical.py` は複数の flash 系モデルをローテーションする
  （`RotatingGeminiClient`、既定 6 モデル。実際に使ったモデル名を各シグナルの
  `generator` に記録）。`llm_client.py` の 429 は retryDelay 尊重 +
  `GeminiRateLimitError`（待機上限時）で呼び出し側が切替判断できる
- LLM 出力の JSON スキーマ違反（risks に object を入れる等）が数%発生するため、
  run_historical は 1 回だけ再サンプルする（それでも失敗した分は resume で再試行）
- yfinance の一部日付に異常値（例: 1306.T 2026-03-30/31 が約1/10 の値）があるため、
  読み込み時に rolling median から 40% 超乖離した行を除外する
