"""PoC-1 サンプルユニバース: 東証プライム大型・流動性の高い11銘柄。

- code: 証券コード4桁。英字入りコード（例: 285A）もあるため文字列として扱うこと。
- news_name: ニュース検索用の通称（省略時は name を使用）。

## universe の差し替え（PoC-2 以降）

`load_universe(path)` で JSON ファイル（例: poc/poc2_stock_universe/universe_50.json）
から universe を読み込める。優先順位:

1. 引数 `path`（各 fetch スクリプトの `--universe` オプション経由）
2. 環境変数 `UNIVERSE_FILE`
3. 上記いずれもなければ従来の 11 銘柄（後方互換）

JSON は次のいずれかの形式を受け付ける:
- `{"stocks": [{"code": ..., "name": ...}, ...]}`（universe_50.json 形式）
- `[{"code": ..., "name": ...}, ...]`（銘柄リスト直接）

銘柄名の全角英数（例: ＫＤＤＩ）はニュース検索でヒットしにくいため、
news_name 未指定時は NFKC 正規化した名前を news_name として補完する。
"""
import json
import os
import unicodedata
from pathlib import Path

UNIVERSE = [
    {"code": "7203", "name": "トヨタ自動車"},
    {"code": "6758", "name": "ソニーグループ"},
    {"code": "8306", "name": "三菱UFJフィナンシャル・グループ"},
    {"code": "9984", "name": "ソフトバンクグループ"},
    {"code": "6861", "name": "キーエンス"},
    {"code": "4063", "name": "信越化学工業"},
    {"code": "9433", "name": "KDDI"},
    {"code": "8058", "name": "三菱商事"},
    {"code": "6501", "name": "日立製作所"},
    {"code": "4568", "name": "第一三共"},
    {"code": "285A", "name": "キオクシアホールディングス", "news_name": "キオクシア"},
]

ENV_UNIVERSE_FILE = "UNIVERSE_FILE"


def _normalize_stock(s: dict) -> dict:
    """JSON の銘柄エントリを {code, name, news_name} 形式に正規化する。"""
    code = str(s["code"])
    name = s["name"]
    stock = {"code": code, "name": name}
    if s.get("news_name"):
        stock["news_name"] = s["news_name"]
    else:
        # 全角英数（ＫＤＤＩ 等）を半角に正規化した名前をニュース検索用に補完
        nfkc = unicodedata.normalize("NFKC", name)
        if nfkc != name:
            stock["news_name"] = nfkc
    return stock


def load_universe(path=None) -> list:
    """universe を返す。path > 環境変数 UNIVERSE_FILE > デフォルト11銘柄 の順で解決。"""
    path = path or os.getenv(ENV_UNIVERSE_FILE)
    if not path:
        return UNIVERSE
    p = Path(path)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    stocks = data["stocks"] if isinstance(data, dict) else data
    if not stocks:
        raise ValueError(f"universe が空です: {p}")
    return [_normalize_stock(s) for s in stocks]


def yf_tickers(universe=None):
    """yfinance 用ティッカー（コード.T）一覧を返す。"""
    return [f"{s['code']}.T" for s in (universe or UNIVERSE)]


def jquants_codes(universe=None):
    """J-Quants 用5桁コード（4桁コード + '0'）一覧を返す。

    英字入りコード（285A → 285A0）にもそのまま適用する（文字列連結のため数値変換しない）。
    """
    return [f"{s['code']}0" for s in (universe or UNIVERSE)]


def edinet_sec_codes(universe=None):
    """EDINET の secCode（5桁 = 4桁コード + '0'）一覧を返す。"""
    return jquants_codes(universe)
