"""PoC-3: LLM クライアントの抽象インターフェース。

プロバイダ・モデル・API キーは未決定のため、実装は stub のみ。
プロバイダ決定後に `complete()` を実装したサブクラスを追加し、
`get_client()` の分岐に登録する。

インターフェースは `complete(system, user) -> str` の 1 メソッドに固定する。
戻り値は「LLM の生テキスト応答」で、JSON パース・検証は呼び出し側
（generate_signal.py）の責務とする。
"""
from abc import ABC, abstractmethod


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


def get_client(provider: str = "stub") -> LLMClient:
    """プロバイダ名からクライアントを生成する。

    プロバイダ実装を追加したら、ここに分岐を追加する。
    例:
        if provider == "anthropic":
            return AnthropicClient(model=..., api_key=...)
    """
    if provider == "stub":
        return StubLLMClient()
    raise ValueError(f"未知のプロバイダ: {provider}（現状 'stub' のみ）")
