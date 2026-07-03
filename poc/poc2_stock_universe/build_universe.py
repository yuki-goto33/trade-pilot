"""PoC-2: 機械的フィルターによる約50銘柄 universe の再現可能な構築。

手順:
  1. J-Quants V2 /equities/master から東証プライムの国内普通株を抽出
     （ETF/REIT/外国株/出資証券は ProdCat で除外、優先株等は5桁コード末尾≠0 で除外）
  2. 直近 20 営業日（無料プランは12週遅延のため「今日-12週」時点が最新）の日足を
     date=YYYYMMDD 指定で取得（1日=1リクエストで全銘柄分が返る）し、
     銘柄ごとの平均売買代金 Va を算出
  3. フィルター: 平均売買代金 >= MIN_AVG_TURNOVER_JPY（流動性下限）
  4. 平均売買代金の降順に選定。ただし 33業種セクター分散制約
     （1業種あたり MAX_PER_SECTOR33 銘柄まで）をかけて TARGET_SIZE 銘柄を選ぶ
  5. 出力: universe_50.json（機械可読） + universe_50.md（業種別の目視レビュー用）

再現性:
  - J-Quants レスポンスは data/poc2_cache/ にキャッシュし、再実行時は API を叩かない
  - 選定ロジックは決定的（ソートキー: 平均売買代金 降順 → コード昇順）
  - 基準日はキャッシュ済みマスタの Date に固定されるため、キャッシュがある限り
    何度実行しても同一の出力になる

レート制限: 無料プランは 5req/分（ローリング60秒窓）→ リクエスト間 15.5 秒。
初回実行は マスタ1 + 日足 約21（祝日1含む）リクエスト ≈ 5〜6 分。
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# フィルターパラメータ（PoC の最終形。変更したら README も更新すること）
# ---------------------------------------------------------------------------
TARGET_SIZE = 50                        # 目標銘柄数
MIN_AVG_TURNOVER_JPY = 5_000_000_000    # 平均売買代金の下限: 50億円/日
MAX_PER_SECTOR33 = 4                    # 33業種 1業種あたりの上限銘柄数
WINDOW_DAYS = 20                        # 平均売買代金の算出対象: 直近20営業日
MIN_PRESENT_DAYS = 15                   # 20営業日中の最低出来日数（新規上場・長期売買停止の除外）

# ---------------------------------------------------------------------------
# J-Quants V2 設定
# ---------------------------------------------------------------------------
BASE_URL = "https://api.jquants.com/v2"
SLEEP_SEC = 15.5      # 5req/分（ローリング窓）対策
DELAY_WEEKS = 12      # 無料プランのデータ遅延

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "data" / "poc2_cache"
OUT_DIR = Path(__file__).resolve().parent

_last_request_at = 0.0


def _rate_limited_get(session: requests.Session, url: str, params: dict) -> requests.Response:
    """15.5秒間隔を保証して GET する。"""
    global _last_request_at
    wait = _last_request_at + SLEEP_SEC - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    r = session.get(url, params=params, timeout=120)
    _last_request_at = time.monotonic()
    return r


def _extract_rows(payload: dict) -> list:
    for v in payload.values():
        if isinstance(v, list):
            return v
    return []


def _fetch_all_pages(session: requests.Session, path: str, params: dict) -> list:
    """pagination_key を辿って全ページを取得する。"""
    rows = []
    params = dict(params)
    while True:
        r = _rate_limited_get(session, f"{BASE_URL}{path}", params)
        r.raise_for_status()
        payload = r.json()
        rows.extend(_extract_rows(payload))
        pk = payload.get("pagination_key")
        if not pk:
            return rows
        params["pagination_key"] = pk


def _cached(name: str, fetch_fn) -> list:
    """data/poc2_cache/<name> があれば読む。なければ fetch_fn() の結果を保存して返す。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows = fetch_fn()
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def make_session() -> requests.Session:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("JQUANTS_API_KEY")
    if not api_key:
        raise RuntimeError("JQUANTS_API_KEY が .env に設定されていません")
    s = requests.Session()
    s.headers.update({"x-api-key": api_key})
    return s


# ---------------------------------------------------------------------------
# 1. 銘柄マスタ: 東証プライム国内普通株の抽出
# ---------------------------------------------------------------------------
def load_master(session: requests.Session) -> list:
    return _cached("master.json", lambda: _fetch_all_pages(session, "/equities/master", {}))


def filter_prime_common(master_rows: list) -> dict:
    """東証プライムの国内普通株のみ {5桁Code: 行} で返す。

    - MktNm == プライム
    - ProdCat == "011"（国内普通株。013=REIT, 014=ETF, 012=出資証券, 021/023=外国株/外国ETF）
    - 5桁コード末尾 "0"（末尾≠0 は優先株等の種類株 → 除外）
    """
    out = {}
    for r in master_rows:
        if r.get("MktNm") != "プライム":
            continue
        if r.get("ProdCat") != "011":
            continue
        code5 = r["Code"]
        if not code5.endswith("0"):
            continue
        out[code5] = r
    return out


# ---------------------------------------------------------------------------
# 2. 直近20営業日の日足を date 指定で取得し、平均売買代金を算出
# ---------------------------------------------------------------------------
def fetch_bars_for_date(session: requests.Session, ymd: str) -> list:
    """指定日の全銘柄日足（祝日・非営業日は空リスト）。キャッシュあり。"""
    return _cached(
        f"bars_{ymd}.json",
        lambda: _fetch_all_pages(session, "/equities/bars/daily", {"date": ymd}),
    )


def collect_trading_days(session: requests.Session, anchor: date, n_days: int) -> dict:
    """anchor から過去に向かって平日を辿り、日足が存在する n_days 営業日分を集める。

    戻り値: {YYYYMMDD: bars_rows}（非営業日は含まない）
    """
    result = {}
    d = anchor
    scanned = 0
    while len(result) < n_days:
        scanned += 1
        if scanned > n_days * 3:
            raise RuntimeError(f"{n_days} 営業日が集まりません（{scanned} 日遡及済み）")
        if d.weekday() < 5:  # 土日はリクエストせずスキップ
            ymd = d.strftime("%Y%m%d")
            rows = fetch_bars_for_date(session, ymd)
            if rows:
                result[ymd] = rows
                print(f"  {ymd}: {len(rows)} 銘柄", flush=True)
            else:
                print(f"  {ymd}: 非営業日（祝日）", flush=True)
        d -= timedelta(days=1)
    return result


def avg_turnover_by_code(bars_by_day: dict) -> dict:
    """{5桁Code: {"avg_va": 平均売買代金, "days": 出来日数}} を返す。

    平均は「値が存在した日」の単純平均。出来日数 < MIN_PRESENT_DAYS は後段で除外。
    """
    acc = {}
    for rows in bars_by_day.values():
        for r in rows:
            va = r.get("Va")
            if va is None:
                continue
            a = acc.setdefault(r["Code"], [0.0, 0])
            a[0] += float(va)
            a[1] += 1
    return {c: {"avg_va": s / n, "days": n} for c, (s, n) in acc.items() if n > 0}


# ---------------------------------------------------------------------------
# 3-4. フィルター + セクター分散制約つき選定
# ---------------------------------------------------------------------------
def select_universe(prime: dict, turnover: dict) -> list:
    """流動性下限 → 売買代金降順 + 33業種上限で TARGET_SIZE 銘柄を選ぶ。"""
    candidates = []
    for code5, m in prime.items():
        t = turnover.get(code5)
        if t is None or t["days"] < MIN_PRESENT_DAYS:
            continue
        if t["avg_va"] < MIN_AVG_TURNOVER_JPY:
            continue
        candidates.append({
            "code": code5[:4],
            "code5": code5,
            "name": m["CoName"],
            "sector33_code": m["S33"],
            "sector33": m["S33Nm"],
            "scale_category": m["ScaleCat"],
            "avg_turnover_jpy": round(t["avg_va"]),
            "days_traded": t["days"],
        })

    # 決定的ソート: 平均売買代金 降順 → 5桁コード 昇順
    candidates.sort(key=lambda x: (-x["avg_turnover_jpy"], x["code5"]))

    selected = []
    sector_count = {}
    for c in candidates:
        if len(selected) >= TARGET_SIZE:
            break
        sc = c["sector33_code"]
        if sector_count.get(sc, 0) >= MAX_PER_SECTOR33:
            continue
        sector_count[sc] = sector_count.get(sc, 0) + 1
        selected.append(c)

    print(f"  流動性フィルター通過: {len(candidates)} 銘柄 → セクター制約後の選定: {len(selected)} 銘柄", flush=True)
    return selected


# ---------------------------------------------------------------------------
# 5. 出力
# ---------------------------------------------------------------------------
def write_outputs(selected: list, trading_days: list, anchor_date: str):
    params = {
        "target_size": TARGET_SIZE,
        "min_avg_turnover_jpy": MIN_AVG_TURNOVER_JPY,
        "max_per_sector33": MAX_PER_SECTOR33,
        "window_days": WINDOW_DAYS,
        "min_present_days": MIN_PRESENT_DAYS,
        "market": "プライム",
        "product_category": "011 (国内普通株)",
    }
    obj = {
        "generated_with": "poc/poc2_stock_universe/build_universe.py",
        "as_of": anchor_date,
        "window": {"from": trading_days[0], "to": trading_days[-1], "trading_days": len(trading_days)},
        "params": params,
        "count": len(selected),
        "stocks": selected,
    }
    (OUT_DIR / "universe_50.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 業種別 Markdown（目視レビュー用）
    by_sector = {}
    for s in selected:
        by_sector.setdefault((s["sector33_code"], s["sector33"]), []).append(s)

    lines = [
        "# Universe 50（目視レビュー用・業種別）",
        "",
        f"- 基準日: {anchor_date}（J-Quants 無料プランの12週遅延データにおける最新営業日）",
        f"- 算出窓: {trading_days[0]}〜{trading_days[-1]} の {len(trading_days)} 営業日",
        f"- フィルター: 東証プライム国内普通株 / 平均売買代金 {MIN_AVG_TURNOVER_JPY / 1e8:.0f}億円/日以上 /"
        f" 売買代金上位から選定（33業種 1業種 {MAX_PER_SECTOR33} 銘柄まで）",
        f"- 銘柄数: {len(selected)}",
        "",
    ]
    for (sc, sn), stocks in sorted(by_sector.items()):
        lines.append(f"## {sn}（{sc}）: {len(stocks)} 銘柄")
        lines.append("")
        lines.append("| コード | 銘柄名 | 平均売買代金 | 規模区分 |")
        lines.append("|---|---|---:|---|")
        for s in sorted(stocks, key=lambda x: -x["avg_turnover_jpy"]):
            lines.append(
                f"| {s['code']} | {s['name']} | {s['avg_turnover_jpy'] / 1e8:,.0f}億円 | {s['scale_category']} |")
        lines.append("")
    (OUT_DIR / "universe_50.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
def main():
    t0 = time.monotonic()
    session = make_session()

    print("1. 銘柄マスタ取得（東証プライム国内普通株の抽出）", flush=True)
    master = load_master(session)
    prime = filter_prime_common(master)
    anchor_date = max(r["Date"] for r in master)  # 無料プランで参照可能な最新営業日
    print(f"  マスタ {len(master)} 件 → プライム普通株 {len(prime)} 銘柄 / 基準日 {anchor_date}", flush=True)

    print(f"2. 直近 {WINDOW_DAYS} 営業日の日足取得（date 指定・1日1リクエスト）", flush=True)
    anchor = datetime.strptime(anchor_date, "%Y-%m-%d").date()
    bars_by_day = collect_trading_days(session, anchor, WINDOW_DAYS)
    trading_days = sorted(bars_by_day.keys())

    print("3-4. 平均売買代金の算出とフィルター適用", flush=True)
    turnover = avg_turnover_by_code(bars_by_day)
    selected = select_universe(prime, turnover)

    print("5. 出力", flush=True)
    write_outputs(selected, trading_days, anchor_date)
    print(f"  {OUT_DIR / 'universe_50.json'}")
    print(f"  {OUT_DIR / 'universe_50.md'}")

    # PoC-1 の 11 銘柄の包含チェック
    poc1 = {"7203": "トヨタ自動車", "6758": "ソニーグループ", "8306": "三菱UFJ FG",
            "9984": "ソフトバンクグループ", "6861": "キーエンス", "4063": "信越化学工業",
            "9433": "KDDI", "8058": "三菱商事", "6501": "日立製作所",
            "4568": "第一三共", "285A": "キオクシアHD"}
    selected_codes = {s["code"] for s in selected}
    print("\nPoC-1 の 11 銘柄の包含チェック:")
    for code, name in poc1.items():
        mark = "IN " if code in selected_codes else "OUT"
        print(f"  [{mark}] {code} {name}")

    print(f"\n完了: {len(selected)} 銘柄 / {time.monotonic() - t0:.0f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
