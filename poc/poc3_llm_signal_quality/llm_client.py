"""PoC-3: LLM クライアントの抽象インターフェース。

現状の実装:
- GeminiClient: Google Gemini API（無料枠、PoC 用）。既定モデルは
  gemini-flash-latest（環境変数 GEMINI_MODEL で変更可）。
- StubLLMClient: 未実装 stub（--dry-run 用）。

インターフェースは `complete(system, user) -> str` の 1 メソッドに固定する。
戻り値は「LLM の生テキスト応答」で、JSON パース・検証は呼び出し側
（generate_signal.py）の責務とする。
"""
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


class GeminiRateLimitError(RuntimeError):
    """429（クォータ超過）の待機上限に達した場合の例外。

    retry_delay_sec に API が提示した待機秒数を保持する（モデルローテーション用）。
    """

    def __init__(self, message: str, retry_delay_sec: float):
        super().__init__(message)
        self.retry_delay_sec = retry_delay_sec


class LLMClient(ABC):
    """LLM 呼び出しの抽象インターフェース。"""

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """システムプロンプトとユーザープロンプトを渡し、応答テキストを返す。

        Args:
            system: システムプロンプト全文。
            user: ユーザープロンプト全文（分析コンテキスト JSON を含む）。

        Returns:
            LLM の応答テキスト（JSON 文字列を期待するが、生テキストのまま返す）。
        """
        raise NotImplementedError


class StubLLMClient(LLMClient):
    """未実装 stub。呼び出すと NotImplementedError を送出する。

    --dry-run（プロンプト出力のみ）では complete() は呼ばれないため、
    パイプラインの他の部分はこの stub のままで検証できる。
    """

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError(
            "LLM プロバイダが未選定のため complete() は未実装です。"
            "プロバイダ・モデル・API キーの決定後に実装クラスを追加してください。"
            "（それまでは generate_signal.py --dry-run を使用）"
        )


class GeminiClient(LLMClient):
    """Google Gemini API クライアント（無料枠での PoC 用）。

    - 認証: `X-goog-api-key` ヘッダー（クエリパラメータだと一部モデルが 404 になる）
    - エンドポイント: v1beta の generateContent
    - JSON モード（responseMimeType）で応答を JSON に制約。スキーマ検証は
      呼び出し側の jsonschema が担う（Gemini の responseSchema は draft-07 の
      if/then 等に非対応のため使わない）
    - 無料枠のレート制限（Flash 系 15req/分・ローリング窓）対策として
      呼び出し間に最小間隔を空け、429 は retryDelay を尊重してリトライする
    """

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    MIN_INTERVAL_SEC = 5.0
    MAX_RETRIES = 4
    # 429（クォータ）はトークンバケット的に数十秒〜数分で回復するため、
    # リトライ回数ではなく累計待機時間で打ち切る（大量バッチ実行用）
    RATE_LIMIT_MAX_WAIT_SEC = 1800.0

    def __init__(self, model: str = None, api_key: str = None,
                 rate_limit_max_wait_sec: float = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise RuntimeError(".env に GEMINI_API_KEY が設定されていません。")
        if rate_limit_max_wait_sec is not None:
            self.RATE_LIMIT_MAX_WAIT_SEC = rate_limit_max_wait_sec
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _retry_delay_sec(body: dict, attempt: int) -> float:
        """429 応答の RetryInfo から待機秒数を取り出す（無ければ指数バックオフ）。"""
        for detail in body.get("error", {}).get("details", []):
            delay = detail.get("retryDelay")  # 例: "37s"
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return float(delay[:-1]) + 1.0
                except ValueError:
                    pass
        return min(20.0 * (2 ** attempt), 120.0)

    def complete(self, system: str, user: str) -> str:
        url = f"{self.BASE_URL}/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": 8192,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-goog-api-key": self.api_key,
        }

        last_err = None
        rate_limit_waited = 0.0
        attempt = 0
        while attempt < self.MAX_RETRIES:
            self._throttle()
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            self._last_call = time.monotonic()

            if resp.status_code == 200:
                data = resp.json()
                try:
                    candidate = data["candidates"][0]
                    parts = candidate.get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                except (KeyError, IndexError) as e:
                    raise RuntimeError(f"Gemini 応答の形式が不正: {data}") from e
                finish = candidate.get("finishReason")
                if finish not in (None, "STOP"):
                    raise RuntimeError(
                        f"Gemini が途中終了 (finishReason={finish})。"
                        f"maxOutputTokens かセーフティ設定を確認してください。"
                    )
                if not text.strip():
                    raise RuntimeError(f"Gemini 応答が空: {json.dumps(data)[:300]}")
                return text

            if resp.status_code == 429 or resp.status_code >= 500:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                delay = self._retry_delay_sec(body, attempt)
                last_err = f"HTTP {resp.status_code}: {str(body)[:200]}"
                if resp.status_code == 429:
                    # クォータ回復待ち: 回数ではなく累計待機時間で管理する
                    if rate_limit_waited + delay > self.RATE_LIMIT_MAX_WAIT_SEC:
                        raise GeminiRateLimitError(
                            f"Gemini API クォータ待機が上限"
                            f"（{self.RATE_LIMIT_MAX_WAIT_SEC:.0f}s）超過"
                            f" (model={self.model}): {last_err}",
                            retry_delay_sec=delay,
                        )
                    rate_limit_waited += delay
                else:
                    attempt += 1
                time.sleep(delay)
                continue

            raise RuntimeError(f"Gemini API エラー HTTP {resp.status_code}: {resp.text[:300]}")

        raise RuntimeError(f"Gemini API リトライ上限到達: {last_err}")


# Gemini 無料枠はモデルごとに小さなトークンバケット型クォータのため、
# 複数モデルをローテーションする（クォータ枯渇時に次モデルへ切替）。
# 敗因分析で lite 系 40.4% vs 非 lite 53.1% だったため非 lite を優先する。
DEFAULT_ROTATION_MODELS = [
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.0-flash-lite",
]


class RotatingGeminiClient(LLMClient):
    """モデルごとの 429 クォータをまたいでローテーションする Gemini クライアント。

    - 各モデルの GeminiClient は 429 を即時 GeminiRateLimitError で返す設定にし、
      クールダウン（API 提示の retryDelay）を記録して次モデルへ切り替える
    - その他の RuntimeError（503 リトライ上限等）も短いクールダウンで次モデルへ
    - 全モデルがクールダウン中なら最短の解除時刻まで待つ
    - 成功したモデル名を last_model に保持する（シグナル記録用）
    """

    TOTAL_WAIT_CAP_SEC = 2700.0   # 1コンテキストあたりの累計待機上限
    ERROR_COOLDOWN_SEC = 90.0     # 429 以外のエラー時のクールダウン

    def __init__(self, models=None):
        self.models = models or DEFAULT_ROTATION_MODELS
        self.clients = {m: GeminiClient(model=m, rate_limit_max_wait_sec=0.0)
                        for m in self.models}
        self.cooldown_until = {m: 0.0 for m in self.models}
        self.last_model = None

    def complete(self, system: str, user: str) -> str:
        import sys
        waited = 0.0
        last_err = None
        while True:
            now = time.monotonic()
            available = [m for m in self.models if self.cooldown_until[m] <= now]
            if not available:
                wait = min(self.cooldown_until.values()) - now + 1.0
                if waited + wait > self.TOTAL_WAIT_CAP_SEC:
                    raise RuntimeError(
                        f"全モデルがクールダウン中で待機上限超過: {last_err}")
                time.sleep(wait)
                waited += wait
                continue
            model = available[0]
            try:
                raw = self.clients[model].complete(system, user)
                self.last_model = model
                return raw
            except GeminiRateLimitError as e:
                self.cooldown_until[model] = (
                    time.monotonic() + max(e.retry_delay_sec, 30.0))
                last_err = str(e)[:150]
                print(f"    [rotate] {model}: quota (cooldown "
                      f"{max(e.retry_delay_sec, 30.0):.0f}s)", file=sys.stderr)
            except RuntimeError as e:
                self.cooldown_until[model] = (
                    time.monotonic() + self.ERROR_COOLDOWN_SEC)
                last_err = str(e)[:150]
                print(f"    [rotate] {model}: {last_err}", file=sys.stderr)


def get_client(provider: str = "stub") -> LLMClient:
    """プロバイダ名からクライアントを生成する。

    'gemini' はモデルローテーション付きクライアントを返す（v3 の専門家モードは
    銘柄あたり 3 コールで単一モデルの無料枠クォータを超えやすいため）。
    単一モデル固定が必要な場合は 'gemini-single' を使う。
    """
    if provider == "stub":
        return StubLLMClient()
    if provider == "gemini":
        return RotatingGeminiClient()
    if provider == "gemini-single":
        return GeminiClient()
    raise ValueError(f"未知のプロバイダ: {provider}（'stub' / 'gemini' / 'gemini-single'）")
