"""OpenAI 形式のエラーに変換できる例外。"""

from __future__ import annotations

from typing import Any, Dict, Optional


class BridgeError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        err_type: str = "api_error",
        code: Optional[str] = None,
        param: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type
        self.code = code
        self.param = param
        # OpenAI 公式 SDK は 5xx を自動リトライするため、リトライしても無駄なエラーは
        # `x-should-retry: false` を返して止める。
        self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": self.param,
                "code": self.code,
            }
        }


def invalid_request(message: str, param: Optional[str] = None, code: Optional[str] = None) -> BridgeError:
    return BridgeError(400, message, "invalid_request_error", code=code, param=param)
