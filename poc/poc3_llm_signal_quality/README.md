# PoC-3: LLM シグナル生成の品質検証（最重要）

テクニカル + 開示/ニュース + マクロを LLM に統合入力し、「買い/売り/様子見 + 確信度 +
目標株価/損切り + 想定保有期間 + 理由」を JSON スキーマ固定で出力させるパイプライン。

**現状は雛形（LLM 呼び出し以外）まで実装済み。** LLM プロバイダ・モデル・API キーが
未決定のため、`llm_client.py` は stub になっている。`--dry-run` でプロンプト生成・
トークン見積もりまでは動作する。

## 構成

| ファイル | 役割 |
|---|---|
| `context_builder.py` | data/ の PoC-1 取得データから 1銘柄分の分析入力（dict/JSON）を組み立てる |
| `prompt_template.md` | システム/ユーザープロンプトのテンプレート（設計方針も記載） |
| `signal_schema.json` | シグナル出力の JSON Schema（draft-07） |
| `llm_client.py` | `complete(system, user) -> str` の抽象インターフェース + stub |
| `generate_signal.py` | コンテキスト → プロンプト → LLM → パース・検証 → 保存 のパイプライン |

パイプラインの流れ:

```
build_context(code)          # data/ からテクニカル・ニュース・開示・マクロを集約
  → render_prompts(context)  # prompt_template.md にコンテキスト JSON とスキーマを埋め込み
  → client.complete(...)     # LLM 呼び出し（現状 stub / NotImplementedError）
  → parse_response(raw)      # JSON パース（防御的にコードフェンス除去）
  → validate_signal(signal)  # signal_schema.json でスキーマ検証
  → save_signal(...)         # data/signals/<YYYY-MM-DD>/<code>.json に保存
```

### コンテキストの内容（context_builder.py）

- **テクニカル**: `prices_yfinance.csv`（日足・直近約30営業日）から pandas のみで計算
  - SMA5 / SMA25（+ ゴールデン/デッドの位置関係）
  - RSI(14)（Wilder 平滑）
  - MACD(12, 26, 9)（約30本での近似値である旨をコンテキストに明記）
  - 直近5営業日の終値・前日比・出来高、期間高値/安値
- **ニュース**: `news_google.json` の該当銘柄見出し（最大10件: タイトル・日時・配信元）
- **開示**: `disclosures_yanoshin.json` の直近開示（最大10件: 日時・タイトル）
- **マクロ**: 日経平均 / TOPIX ETF(1306) / ドル円 / S&P500 / VIX の直近値と1日・5日変化率
  （`macro_yfinance.csv`）+ 日本10年金利（`macro_jgb.json`）+ 米10年金利・FF金利（`macro_fred.json`）

### 出力スキーマ（signal_schema.json）

`signal(buy/sell/hold)` / `confidence(0-100)` / `target_price` / `stop_loss` /
`holding_period_days(1-365)` / `reasons[]（根拠 + 参照データ evidence）` / `risks[]`。
全フィールド必須・`additionalProperties: false`。buy/sell のときは
target_price / stop_loss を数値必須（hold のみ null 可）とする条件付き制約入り。

### プロンプト設計の方針（prompt_template.md）

1. **役割定義**: 日本株スイング〜中長期のアナリスト（デイトレ除外）
2. **ハルシネーション対策**: 入力データにある事実のみで判断、タイトルしかない情報から
   本文内容を推測することを禁止、各根拠に `evidence`（参照した入力データの箇所）を必須化
3. **出力固定**: スキーマをシステムプロンプトに埋め込み、JSON のみ出力（フェンス・前置き禁止）
4. **判断基準ガイドライン**: buy/sell/hold の目安（複数根拠 + リスクリワード 1.5 以上で buy 等）、
   confidence の付け方の制約、**迷ったら hold** に倒す方針

## 実行方法

```bash
# 前提: PoC-1 のデータが data/ にあること（無ければ poc1_data_sources/run_all.py）
#       .venv に jsonschema が入っていること（requirements.txt に追加済み）

cd poc/poc3_llm_signal_quality

# dry-run: LLM に送るプロンプト全文を data/signals/dry_run/ に出力 + トークン見積もり
../../.venv/bin/python generate_signal.py --dry-run

# 特定銘柄のみ / コンテキストの単体確認
../../.venv/bin/python generate_signal.py --dry-run --codes 7203 6758
../../.venv/bin/python context_builder.py 7203

# 本実行（プロバイダ実装後。現状は NotImplementedError で停止する）
../../.venv/bin/python generate_signal.py --provider <name>
```

## dry-run 結果とトークン見積もり（2026-07-04 実測）

universe 11銘柄で `--dry-run` を実行。トークンは粗い換算
（**日本語等の非 ASCII 1文字 = 1トークン / ASCII 4文字 = 1トークン**）による概算。

| code | 銘柄 | system | user | 入力計 |
|---|---|---:|---:|---:|
| 7203 | トヨタ自動車 | 1,778 | 1,880 | 3,658 |
| 6758 | ソニーグループ | 1,778 | 1,993 | 3,771 |
| 8306 | 三菱UFJ FG | 1,778 | 1,693 | 3,471 |
| 9984 | ソフトバンクグループ | 1,778 | 2,018 | 3,796 |
| 6861 | キーエンス | 1,778 | 1,790 | 3,568 |
| 4063 | 信越化学工業 | 1,778 | 1,881 | 3,659 |
| 9433 | KDDI | 1,778 | 1,854 | 3,632 |
| 8058 | 三菱商事 | 1,778 | 1,940 | 3,718 |
| 6501 | 日立製作所 | 1,778 | 1,953 | 3,731 |
| 4568 | 第一三共 | 1,778 | 1,926 | 3,704 |
| 285A | キオクシア HD | 1,778 | 1,962 | 3,740 |

- **平均入力: 約 3,700 トークン/銘柄**（system 約 1,800 = 指示 + スキーマ、user 約 1,900 = コンテキスト JSON）
- **想定出力: 約 500 トークン/銘柄**（スキーマ準拠の応答 JSON: 根拠3件 + リスク3件程度の想定値）

### 月間トークン量（50銘柄 × 日次 × 22営業日 = 1,100回/月）

| 区分 | 月間トークン量（概算） |
|---|---:|
| 入力 | **約 405万 トークン**（3,677 × 1,100 = 4,044,700） |
| 出力 | **約 55万 トークン**（500 × 1,100 = 550,000） |

単価はモデル決定後に掛ける（成功基準: 月 5,000 円以内）。system プロンプトは全銘柄共通
（約 1,800 トークン）のため、プロンプトキャッシュが効くモデルなら入力の約半分にキャッシュ
割引が効く余地がある。集計の生データは `data/signals/dry_run/summary.json`。

## 残タスク（PoC-3 本体）

- [ ] **LLM プロバイダ・モデル・API キーの決定**（ユーザー確認待ち）→ `llm_client.py` に実装クラス追加
- [ ] 数銘柄で試走し、フォーマット遵守率・理由の品質を確認、プロンプト調整
- [ ] 実測トークン数・実コストで見積もりを更新（月 5,000 円以内の確認）
- [ ] フォワードテスト 4 週間（毎朝生成・記録 → `data/signals/` に蓄積）
- [ ] 簡易ヒストリカルテスト 30 ケース以上（J-Quants 過去データ + リーク注意）
- [ ] 理由の人手評価 30 件以上（事実誤認・納得感の採点）

## 実装状況（2026-07-04 更新）

- **LLM プロバイダ実装済み**: `GeminiClient`（Google Gemini API 無料枠、既定モデル `gemini-flash-latest`、`GEMINI_MODEL` で変更可）
  - 認証は `X-goog-api-key` ヘッダー（クエリパラメータ方式は一部モデルが 404 になるため不可）
  - JSON モード（responseMimeType）+ ローカル jsonschema 検証。レート制限対策: 呼び出し間隔 5 秒 + 429 時は retryDelay 尊重リトライ
  - この API キーの無料枠では Pro 系（gemini-2.5-pro 等）は quota 0 で利用不可。Flash 系のみ
- **フォワードテスト開始**: 2026-07-04 に全 11 銘柄のシグナル生成成功（`data/signals/2026-07-04/`）
- 実行: `../../.venv/bin/python generate_signal.py --provider gemini`
- 既知の注意点:
  - 同一入力でも判定が揺れる（トヨタで buy75 → hold60 を観測）。フォワードテストでは 1 日 1 回の生成値を正とする
  - `data/` の取得ファイルは universe 間で共有のため、50 銘柄検証などを走らせた後は 11 銘柄で取得し直すこと（要改善: universe 別ファイル名）
