"""PoC-1 共通ユーティリティ。"""
import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

JST = timezone(timedelta(hours=9))


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def save_json(name: str, obj) -> Path:
    """data/<name> に JSON を保存してパスを返す。"""
    ensure_data_dir()
    path = DATA_DIR / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return path


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


class Timer:
    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.monotonic() - self.start, 1)


def parse_universe_arg(description: str) -> "str | None":
    """fetch スクリプト共通の --universe オプションをパースしてパスを返す。"""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument(
        "--universe",
        default=None,
        help="universe JSON のパス（省略時: 環境変数 UNIVERSE_FILE → デフォルト11銘柄）",
    )
    return ap.parse_args().universe


def print_summary(source: str, ok: bool, count: int, seconds: float, note: str = ""):
    status = "OK " if ok else "FAIL"
    line = f"[{status}] {source}: {count} 件 / {seconds}s"
    if note:
        line += f" ({note})"
    print(line)
