"""リクエストを Cursor CLI で実行し、テキスト差分・関数呼び出し・最終結果に整形する。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional, Tuple

from .config import Config
from .cursor_cli import CursorCLI
from .errors import BridgeError
from .prompt import (BridgeRequest, ToolCall, clean_json_output, estimate_tokens, parse_tool_calls,
                     render_prompt)

log = logging.getLogger("cursor_openai_api")

DEFAULT_ALIASES = ("default", "cursor-default")
BUFFER_KEEPALIVE_SECONDS = 15.0


@dataclass
class FinalResult:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cursor_model: Optional[str] = None
    session_id: Optional[str] = None
    duration_ms: int = 0

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.tool_calls else "stop"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# engine.run() が返すイベント:
#   ("text", str)          … クライアントへ流してよいテキスト差分
#   ("keepalive", None)    … まだ出力がない（接続維持用）
#   ("final", FinalResult) … 完了
Event = Tuple[str, Any]


class Engine:
    def __init__(self, cfg: Config, cli: Optional[CursorCLI] = None) -> None:
        self.cfg = cfg
        self.cli = cli or CursorCLI(cfg)

    # ------------------------------------------------------------------ モデル
    def resolve_model(self, requested: Optional[str]) -> Optional[str]:
        """クライアントが指定したモデル名を、Cursor CLI の --model に渡す値へ変換する（None は指定なし）。"""
        name = (requested or "").strip()
        if not name or name in DEFAULT_ALIASES:
            return self.cfg.default_model
        if name in self.cfg.model_map:
            target = self.cfg.model_map[name]
            return self.cfg.default_model if target in DEFAULT_ALIASES else target
        known = self.cli.known_model_ids() + self.cfg.extra_models
        if not known:
            return name  # 一覧が取れないときはそのまま渡す
        if name in known:
            return name
        if self.cfg.strict_models:
            raise BridgeError(404, f"モデル '{name}' は Cursor で使えません。/v1/models で一覧を確認するか、"
                                   "MODEL_MAP で読み替えを設定してください。",
                              "invalid_request_error", code="model_not_found", param="model")
        log.info("モデル '%s' は Cursor に無いため既定モデル（%s）で実行します", name, self.cfg.default_model or "CLI の既定")
        return self.cfg.default_model

    # ------------------------------------------------------------------ 実行
    def run(self, req: BridgeRequest, model: Optional[str], request_id: str,
            build_prompt: Optional[str] = None) -> Iterator[Event]:
        prompt = build_prompt if build_prompt is not None else render_prompt(req, preamble=self.cfg.preamble)
        run = self.cli.start_run(prompt, model, req.attachments, request_id)
        try:
            mode = "json" if req.response_format.kind != "text" else ("tools" if req.tools_active else "plain")
            streaming = mode == "plain"
            decided = streaming
            full = ""
            result = None
            last_signal = time.time()
            for kind, payload in run.events():
                if kind == "keepalive":
                    last_signal = time.time()
                    yield ("keepalive", None)
                    continue
                if kind == "done":
                    result = payload
                    break
                full += payload
                if streaming:
                    yield ("text", payload)
                    continue
                if time.time() - last_signal >= BUFFER_KEEPALIVE_SECONDS:
                    # 応答を溜めている間も定期的に信号を出し、クライアントの切断を検知できるようにする
                    last_signal = time.time()
                    yield ("keepalive", None)
                if mode == "tools" and not decided:
                    head = full.lstrip()
                    if head:
                        decided = True
                        # 関数呼び出しは JSON で返す約束なので、'{' や '`' で始まる場合は最後まで溜めて判定する
                        if head[0] not in "{`[":
                            streaming = True
                            yield ("text", full)

            content = full
            calls: List[ToolCall] = []
            if mode == "tools":
                names = [t.name for t in req.tools]
                rest, parsed = parse_tool_calls(full, names)
                if parsed:
                    calls = parsed
                    if not streaming:
                        content = rest
                        if rest:
                            yield ("text", rest)
                elif not streaming:
                    yield ("text", full)
            elif mode == "json":
                content = clean_json_output(full)
                yield ("text", content)

            completion_text = content + "".join(c.name + c.arguments for c in calls)
            yield ("final", FinalResult(
                content=content,
                tool_calls=calls,
                prompt_tokens=estimate_tokens(prompt),
                completion_tokens=estimate_tokens(completion_text),
                cursor_model=getattr(result, "cursor_model", None),
                session_id=getattr(result, "session_id", None),
                duration_ms=getattr(result, "duration_ms", 0),
            ))
        finally:
            run.close()

    def complete(self, req: BridgeRequest, model: Optional[str], request_id: str) -> FinalResult:
        final: Optional[FinalResult] = None
        for kind, payload in self.run(req, model, request_id):
            if kind == "final":
                final = payload
        assert final is not None
        return final
