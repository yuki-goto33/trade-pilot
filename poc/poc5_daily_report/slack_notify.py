"""PoC-5: Slack Incoming Webhook への通知。

- Webhook URL は環境変数 `SLACK_WEBHOOK_URL`（.env でも可）から取得
- 分割メッセージ（リスト）に対応し、順番に POST する
- 失敗しても例外は投げず、stderr に出力して False を返す
- URL 未設定時は stdout に本文を出力するフォールバック（開発・PoC 用）
"""
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

POST_TIMEOUT_SEC = 30
SLEEP_BETWEEN_SEC = 1.0


def send_messages(messages, webhook_url=None) -> bool:
    """メッセージ（str または str のリスト）を Slack に送信する。

    Returns:
        True: 全メッセージ送信成功（URL 未設定の stdout フォールバック含む）
        False: 1 件以上の送信失敗（詳細は stderr）
    """
    if isinstance(messages, str):
        messages = [messages]

    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print(
            "[INFO] SLACK_WEBHOOK_URL が未設定のため stdout に出力します。",
            file=sys.stderr,
        )
        for i, msg in enumerate(messages, 1):
            print(f"----- Slack メッセージ {i}/{len(messages)} -----")
            print(msg)
        return True

    ok = True
    for i, msg in enumerate(messages, 1):
        if i > 1:
            time.sleep(SLEEP_BETWEEN_SEC)
        try:
            resp = requests.post(url, json={"text": msg}, timeout=POST_TIMEOUT_SEC)
            if resp.status_code != 200:
                print(
                    f"[NG] Slack 送信失敗 ({i}/{len(messages)}): "
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    file=sys.stderr,
                )
                ok = False
            else:
                print(f"[OK] Slack 送信 {i}/{len(messages)} ({len(msg)} 字)")
        except requests.RequestException as e:
            print(
                f"[NG] Slack 送信失敗 ({i}/{len(messages)}): {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            ok = False
    return ok


if __name__ == "__main__":
    # 疎通テスト: python slack_notify.py "テストメッセージ"
    text = sys.argv[1] if len(sys.argv) > 1 else "trade-pilot PoC-5 疎通テスト :white_check_mark:"
    sys.exit(0 if send_messages(text) else 1)
