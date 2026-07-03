# PoC-1: 無料データソースの取得検証

日本株投資支援ツールの日次シグナル生成に必要な 5 カテゴリ（株価/財務/開示/ニュース/マクロ）について、
無料ソースからサンプルユニバース 11 銘柄分のデータを実際に取得できるか検証するスクリプト群。

## サンプルユニバース（11銘柄・東証プライム）

`universe.py` で定義し全スクリプトで共有。
トヨタ自動車(7203) / ソニーグループ(6758) / 三菱UFJ FG(8306) / ソフトバンクグループ(9984) /
キーエンス(6861) / 信越化学工業(4063) / KDDI(9433) / 三菱商事(8058) / 日立製作所(6501) /
第一三共(4568) / キオクシアホールディングス(285A)

285A は英字入り証券コードのエッジケースとして追加。コードは常に文字列として扱い、
5桁コード変換は文字列連結（`285A` → `285A0`）で行う。

## スクリプト一覧

| スクリプト | ソース | 内容 | 出力（data/ 配下） |
|---|---|---|---|
| `fetch_prices_yfinance.py` | yfinance（非公式） | 日足 OHLCV 直近30日 + 調整後終値。バッチ取得、失敗銘柄は 1req/秒で個別リトライ | `prices_yfinance.csv` |
| `fetch_prices_jquants.py` | J-Quants API V2 無料プラン | 過去2年日足（調整済）。`x-api-key` 認証、15.5秒間隔 | `prices_jquants.csv` ほか |
| `fetch_financials_edinet.py` | EDINET API v2 | 直近30日の提出書類一覧から universe 銘柄を抽出 + type=5 CSV を1件 実DL・pandas 読込検証 | `financials_edinet.json` ほか |
| `fetch_disclosures_yanoshin.py` | yanoshin TDnet WEB-API（非公式） | 銘柄別の直近適時開示一覧（最大50件/銘柄）。3.5秒間隔 | `disclosures_yanoshin.json` |
| `fetch_news_google.py` | Google News RSS | 「"社名" OR "コード" when:1d」の銘柄別ニュース。2秒間隔 | `news_google.json` |
| `fetch_news_macro_rss.py` | NHK経済 RSS + Yahoo!ニュース経済 RSS | マクロ・市況ニュース見出し | `news_macro_rss.json` |
| `fetch_macro.py` | yfinance + 財務省 + FRED | ^N225/1306.T/JPY=X/^GSPC/^VIX + TOPIX-17業種ETF(1617–1633.T) 直近5日、財務省 jgbcm.csv から10年金利（和暦→西暦変換）、FRED DGS10/DFF | `macro_yfinance.csv`, `macro_jgb.json`, `macro_fred.json` |
| `run_all.py` | — | 上記を順に実行し成否一覧を出力 | `run_summary.json` |

共通方針: ソース単位で失敗してもスキップして継続 / リクエスト間に数秒スリープ / 結果は data/（gitignore 済み）に保存。

## セットアップと実行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # JQUANTS_API_KEY / EDINET_API_KEY / FRED_API_KEY を設定

cd poc/poc1_data_sources
../../.venv/bin/python run_all.py          # 全ソース一括（約6分）
../../.venv/bin/python fetch_macro.py      # 個別実行も可
```

## 実行結果サマリー（2026-07-04 実測・全ソース成功）

| ソース | 成否 | 件数 | 所要時間 |
|---|---|---:|---:|
| prices_yfinance | OK | 319 行（11/11銘柄 × 約30日） | 約3秒 |
| prices_jquants | OK | 5,198 行（2024-04-11〜2026-04-10） | 約160秒 |
| financials_edinet | OK | 95 書類（30日分）+ CSV検証OK | 約118秒 |
| disclosures_yanoshin | OK | 538 件（11/11銘柄） | 約38秒 |
| news_google | OK | 約300 件（11/11銘柄） | 約28秒 |
| news_macro_rss | OK | 115 件（NHK 65 + Yahoo 50） | 約3秒 |
| macro | OK | yfinance 22系列109行 + JGB10年 + FRED 10観測 | 約6秒 |

キオクシア(285A)も全ソースで取得確認済み: yfinance 285A.T ✓ / J-Quants 285A0 で318行（2024-12上場のため期間短め）✓ / yanoshin 38件 ✓ / Google News 100件 ✓ / EDINET secCode=285A0 ✓。

## 実測で判明した制約・気づき

- **J-Quants 無料プランの購読範囲は「(今日−12週−2年) 〜 (今日−12週)」**。範囲外の日付を
  `from`/`to` に指定すると 400（メッセージに購読範囲が入る）。スクリプトは範囲をクランプし、
  400 時はエラーメッセージから範囲をパースして1回リトライする。
- **J-Quants のレート制限 5req/分はローリング60秒窓**。13秒間隔だと窓内に5リクエスト入り
  429 になった（実測）→ 15.5秒間隔に設定。
- **財務省 jgbcm.csv は当月分のみ**（月初は数行）。長期履歴が必要なら `jgbcm_all.csv` を使う。
  エンコーディングは cp932、日付は和暦（例: `R8.7.2`）で M/T/S/H/R の元号変換が必要。
- **EDINET type=5 の CSV は ZIP 内に UTF-16LE・タブ区切り**で格納されている。
  認証はクエリパラメータ `Subscription-Key`。一覧は日付単位取得のため30日分で30リクエスト必要。
- **yfinance は auto_adjust=False を明示**しないと Adj Close 列が出ない（現行デフォルトは True）。
  直近バーが NaN になる事象があるため dropna 必須。バッチ（threads=False）で 11銘柄 2〜3秒。
- **Google News RSS は when:1d でも銘柄により 9〜100 件**とばらつきが大きい。値動きの激しい
  銘柄（キオクシア等）は 100 件上限に張り付く。
- 非公式ソース（yfinance / Google News / yanoshin）は突然停止しうる前提で、
  全スクリプトがソース単位のスキップ・継続設計になっている。
