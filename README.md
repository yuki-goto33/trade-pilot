# trade-pilot

AI が日本株（〜50銘柄）の情報を自動収集・分析し、理由付きの売買シグナルを毎朝レポートする投資意思決定支援ツール。

現在は **PoC（概念実証）フェーズ**。企画・検証計画のドキュメントは Ideas リポジトリで管理している。

## PoC 項目

| # | ディレクトリ | 検証内容 |
|---|-------------|---------|
| PoC-1 | `poc/poc1_data_sources/` | 無料データソースの調査・選定と収集パイプライン試作 |
| PoC-2 | `poc/poc2_stock_universe/` | 銘柄フィルタリング条件の設計と universe 構築 |
| PoC-3 | `poc/poc3_llm_signal_quality/` | LLM シグナル生成の品質検証（最重要） |
| PoC-4 | `poc/poc4_backtest_framework/` | 日本株バックテスト基盤の選定・検証 |
| PoC-5 | `poc/poc5_daily_report/` | デイリーレポート生成・配信の試作 |
| PoC-6 | `poc/poc6_ai_chat/` | AI チャット（pull 型 Q&A）の検証 |

## 注意

- 本リポジトリは public のため、**API キー等の認証情報は絶対にコミットしない**（`.env` は gitignore 済み）
- 本ツールは投資判断の支援を目的とし、最終的な投資判断は利用者自身が行う
