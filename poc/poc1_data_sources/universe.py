"""PoC-1 サンプルユニバース: 東証プライム大型・流動性の高い11銘柄。

- code: 証券コード4桁。英字入りコード（例: 285A）もあるため文字列として扱うこと。
- news_name: ニュース検索用の通称（省略時は name を使用）。
"""

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


def yf_tickers():
    """yfinance 用ティッカー（コード.T）一覧を返す。"""
    return [f"{s['code']}.T" for s in UNIVERSE]


def jquants_codes():
    """J-Quants 用5桁コード（4桁コード + '0'）一覧を返す。

    英字入りコード（285A → 285A0）にもそのまま適用する（文字列連結のため数値変換しない）。
    """
    return [f"{s['code']}0" for s in UNIVERSE]


def edinet_sec_codes():
    """EDINET の secCode（5桁 = 4桁コード + '0'）一覧を返す。"""
    return jquants_codes()
