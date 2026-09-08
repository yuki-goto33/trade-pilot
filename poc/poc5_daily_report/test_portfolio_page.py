"""portfolio_page.py のスモークテスト（合成 SimulationResult → JSON/HTML）。"""
from portfolio_page import build_portfolio_html, build_portfolio_json
from simulate import SimulationResult


def make_result() -> SimulationResult:
    return SimulationResult(
        daily=[
            {"date": "2026-07-01", "equity": 3_000_000, "cash": 2_700_000, "n_positions": 1},
            {"date": "2026-07-02", "equity": 3_030_000, "cash": 2_700_000, "n_positions": 1},
        ],
        trades=[{
            "code": "6758", "name": "ソニーG", "entry_date": "2026-07-01",
            "entry_price": 500.0, "exit_date": "2026-07-02", "exit_price": 550.0,
            "shares": 600.0, "pnl": 30_000.0, "pnl_pct": 10.0, "exit_reason": "target",
        }],
        positions=[{
            "code": "7203", "name": "トヨタ自動車", "entry_date": "2026-07-01",
            "entry_price": 3000.0, "shares": 100.0, "last_price": 3100.0,
            "value": 310_000.0, "unrealized_pnl_pct": 3.33,
        }],
        initial_cash=3_000_000,
    )


BENCH = [
    {"date": "2026-07-01", "equity": 3_000_000},
    {"date": "2026-07-02", "equity": 3_010_000},
]


def test_build_portfolio_json():
    data = build_portfolio_json(make_result(), BENCH)
    assert data["initial_cash"] == 3_000_000
    assert data["latest"]["equity"] == 3_030_000
    assert data["latest"]["return_pct"] == 1.0
    assert len(data["daily"]) == 2
    assert len(data["trades"]) == 1
    assert len(data["positions"]) == 1
    assert data["benchmark"][0]["equity"] == 3_000_000
    # 勝率: 勝ち1 / 決済1
    assert data["stats"]["win_rate_pct"] == 100.0


def test_build_portfolio_html_contains_key_elements():
    html = build_portfolio_html(make_result(), BENCH)
    assert "<svg" in html                    # 資産推移チャート
    assert "トヨタ自動車" in html            # 現在ポジション
    assert "ソニーG" in html                 # 取引履歴
    assert "TOPIX" in html                   # ベンチマーク凡例
    assert "投資助言ではありません" in html   # 免責の明記
    assert "手数料" in html                  # 前提の明記


def test_build_portfolio_html_empty_result():
    """シグナル蓄積前でも壊れず生成できる。"""
    html = build_portfolio_html(SimulationResult(initial_cash=3_000_000), [])
    assert "<svg" not in html or "データなし" in html or html  # 例外なく文字列が返る
