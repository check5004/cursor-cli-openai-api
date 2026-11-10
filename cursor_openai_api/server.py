"""OpenAI 互換の HTTP サーバー（Python 標準ライブラリのみ）。"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .config import Config
from .cursor_cli import attachment_ref
from .engine import Engine, FinalResult
from .errors import BridgeError, invalid_request
from .openai_compat import (chat_chunk, chat_completion, chat_request_from_body, chat_usage_chunk,
                            final_output_items, function_call_item, new_turns_for_history, now, output_text_part,
                            output_turn, response_message_item, response_object, responses_request_from_body)
from .prompt import Turn, new_id

log = logging.getLogger("cursor_openai_api")

MAX_BODY_BYTES = 64 * 1024 * 1024
KNOWN_PATHS = ("/chat/completions", "/models", "/responses", "/completions", "/embeddings")


class ClientGone(Exception):
    """クライアントが切断した。"""


class ResponseStore:
    """previous_response_id 用に、Responses API の結果をメモリに保持する（再起動で消える）。"""

    def __init__(self, max_items: int = 500) -> None:
        self.max_items = max_items
        self._data: "OrderedDict[str, Tuple[Dict[str, Any], List[Turn]]]" = OrderedDict()
        self._lock = threading.Lock()

    def put(self, response: Dict[str, Any], history: List[Turn]) -> None:
        with self._lock:
            self._data[response["id"]] = (response, history)
            self._data.move_to_end(response["id"])
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def get(self, response_id: str) -> Optional[Tuple[Dict[str, Any], List[Turn]]]:
        with self._lock:
            return self._data.get(response_id)

    def delete(self, response_id: str) -> bool:
        with self._lock:
            return self._data.pop(response_id, None) is not None


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, cfg: Config, engine: Optional[Engine] = None) -> None:
        self.cfg = cfg
        self.engine = engine or Engine(cfg)
        self.store = ResponseStore()
        super().__init__((cfg.host, cfg.port), Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return  # クライアント側が接続を閉じただけなので無視する
        super().handle_error(request, client_address)


class SSEWriter:
    """Server-Sent Events をチャンク転送で書き出す。"""

    def __init__(self, handler: "Handler", extra_headers: Dict[str, str]) -> None:
        self.h = handler
        self.extra_headers = extra_headers
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        h = self.h
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Transfer-Encoding", "chunked")
        h.send_header("X-Accel-Buffering", "no")
        for k, v in self.extra_headers.items():
            h.send_header(k, v)
        h.send_cors_headers()
        h.end_headers()
        h.status_for_log = 200

    def _write(self, data: bytes) -> None:
        try:
            self.h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            raise ClientGone() from exc

    def data(self, obj: Any, event: Optional[str] = None) -> None:
        payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        text = (f"event: {event}\n" if event else "") + f"data: {payload}\n\n"
        self._write(text.encode("utf-8"))

    def comment(self, text: str) -> None:
        self._write(f": {text}\n\n".encode("utf-8"))

    def end(self) -> None:
        try:
            self.h.wfile.write(b"0\r\n\r\n")
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"cursor-openai-api/{__version__}"
    server: BridgeServer

    status_for_log = 0

    # ------------------------------------------------------------------ 基本
    @property
    def cfg(self) -> Config:
        return self.server.cfg

    @property
    def engine(self) -> Engine:
        return self.server.engine

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.cfg.verbose:
            log.debug("%s - %s", self.address_string(), format % args)

    def send_cors_headers(self) -> None:
        origin = self.cfg.cors_origin
        if not origin:
            return
        req_origin = self.headers.get("Origin")
        allowed = [o.strip() for o in origin.split(",") if o.strip()]
        if "*" in allowed:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif req_origin and req_origin in allowed:
            self.send_header("Access-Control-Allow-Origin", req_origin)
            self.send_header("Vary", "Origin")
        else:
            return
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers") or "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")

    def send_json(self, status: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_cors_headers()
        self.end_headers()
        self.status_for_log = status
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    def send_error_json(self, err: BridgeError, headers: Optional[Dict[str, str]] = None) -> None:
        hdrs = dict(headers or {})
        if not err.retryable:
            hdrs["x-should-retry"] = "false"
        self.send_json(err.status, err.to_dict(), hdrs)

    def route_path(self) -> str:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not path.startswith("/v1/") and path != "/v1":
            for known in KNOWN_PATHS:
                if path == known or path.startswith(known + "/"):
                    return "/v1" + path
        return path

    def check_auth(self) -> bool:
        key = self.cfg.api_key
        if not key:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = token or self.headers.get("api-key", "") or self.headers.get("x-api-key", "")
        if token and hmac.compare_digest(token.encode(), key.encode()):
            return True
        self.send_error_json(BridgeError(401, "API キーが正しくありません（Authorization: Bearer <BRIDGE_API_KEY>）。",
                                         "invalid_request_error", code="invalid_api_key"))
        return False

    def read_json(self) -> Dict[str, Any]:
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raw = self._read_chunked()
        else:
            try:
                n = int(length or 0)
            except ValueError:
                raise invalid_request("Content-Length が不正です。")
            if n > MAX_BODY_BYTES:
                raise BridgeError(413, "リクエストボディが大きすぎます。", "invalid_request_error")
            raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            raise invalid_request("JSON ボディが空です。")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise invalid_request("JSON として解釈できませんでした。")
        if not isinstance(body, dict):
            raise invalid_request("JSON オブジェクトを送ってください。")
        return body

    def _read_chunked(self) -> bytes:
        data = b""
        while True:
            line = self.rfile.readline(65537).strip()
            size = int(line.split(b";")[0] or b"0", 16)
            if size == 0:
                while self.rfile.readline(65537).strip():
                    pass
                return data
            data += self.rfile.read(size)
            self.rfile.readline()
            if len(data) > MAX_BODY_BYTES:
                raise BridgeError(413, "リクエストボディが大きすぎます。", "invalid_request_error")

    # ------------------------------------------------------------------ HTTP メソッド
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        started = time.time()
        path = self.route_path()
        self.request_id = new_id("req_", 16)
        self.log_model = ""
        try:
            if path in ("/", "/health", "/healthz", "/v1/health"):
                self.send_json(200, {"status": "ok", "version": __version__})
                return
            if not self.check_auth():
                return
            if method == "GET" and path == "/v1/models":
                self.handle_models()
            elif method == "GET" and path.startswith("/v1/models/"):
                self.handle_model(path[len("/v1/models/"):])
            elif method == "POST" and path == "/v1/chat/completions":
                self.handle_chat(self.read_json())
            elif method == "POST" and path == "/v1/responses":
                self.handle_responses(self.read_json())
            elif method == "GET" and path.startswith("/v1/responses/"):
                self.handle_get_response(path[len("/v1/responses/"):])
            elif method == "DELETE" and path.startswith("/v1/responses/"):
                rid = path[len("/v1/responses/"):]
                if self.server.store.delete(rid):
                    self.send_json(200, {"id": rid, "object": "response.deleted", "deleted": True})
                else:
                    raise BridgeError(404, f"Response '{rid}' が見つかりません。", "invalid_request_error", code="not_found")
            elif path in ("/v1/completions", "/v1/embeddings", "/v1/images/generations", "/v1/audio/transcriptions"):
                raise BridgeError(501, f"{path} はこのブリッジでは未対応です（chat/completions と responses のみ対応）。",
                                  "invalid_request_error", code="unsupported_endpoint")
            else:
                raise BridgeError(404, f"Unknown path: {method} {path}", "invalid_request_error", code="unknown_url")
        except BridgeError as err:
            self.send_error_json(err)
        except ClientGone:
            self.close_connection = True
            log.info("[%s] クライアントが切断しました", self.request_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 予期しないエラー", self.request_id)
            try:
                self.send_error_json(BridgeError(500, f"内部エラー: {exc}", code="internal_error"))
            except Exception:  # noqa: BLE001
                pass
        finally:
            ms = int((time.time() - started) * 1000)
            if path not in ("/health", "/healthz", "/"):
                log.info("%s %s -> %s (%d ms)%s", method, path, self.status_for_log or "-", ms,
                         f" model={self.log_model}" if self.log_model else "")

    # ------------------------------------------------------------------ /v1/models
    def _model_entries(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        seen = set()

        def add(mid: str, owned_by: str, **extra: Any) -> None:
            if mid in seen:
                return
            seen.add(mid)
            entries.append({"id": mid, "object": "model", "created": 0, "owned_by": owned_by, **extra})

        add("default", "cursor", description=f"Cursor CLI の既定モデル（{self.cfg.default_model or '--model 指定なし'}）")
        for alias, target in self.cfg.model_map.items():
            add(alias, "cursor-alias", description=f"→ {target}")
        for m in self.engine.cli.list_models():
            add(m["id"], "cursor", display_name=m.get("name", m["id"]))
        for mid in self.cfg.extra_models:
            add(mid, "cursor")
        return entries

    def handle_models(self) -> None:
        self.send_json(200, {"object": "list", "data": self._model_entries()})

    def handle_model(self, model_id: str) -> None:
        for entry in self._model_entries():
            if entry["id"] == model_id:
                self.send_json(200, entry)
                return
        raise BridgeError(404, f"The model '{model_id}' does not exist", "invalid_request_error", code="model_not_found",
                          param="model")

    # ------------------------------------------------------------------ 共通ヘルパー
    def _ref(self, filename: str) -> str:
        return attachment_ref(self.cfg, self.request_id, filename)

    def _resolve_model(self, body: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        requested = body.get("model") or "default"
        if not isinstance(requested, str):
            raise invalid_request("model は文字列で指定してください。", param="model")
        resolved = self.engine.resolve_model(requested)
        self.log_model = f"{requested}->{resolved or 'default'}"
        return requested, resolved

    def _common_headers(self, resolved: Optional[str]) -> Dict[str, str]:
        return {"x-request-id": self.request_id, "x-cursor-model": resolved or "default"}

    # ------------------------------------------------------------------ /v1/chat/completions
    def handle_chat(self, body: Dict[str, Any]) -> None:
        n = body.get("n", 1)
        if isinstance(n, int) and n > 1:
            raise invalid_request("n > 1 は未対応です。", param="n")
        requested, resolved = self._resolve_model(body)
        req = chat_request_from_body(body, self._ref)
        legacy = bool(body.get("functions")) and not body.get("tools")
        completion_id = new_id("chatcmpl-", 29)
        created = now()
        headers = self._common_headers(resolved)

        if not body.get("stream"):
            final = self.engine.complete(req, resolved, self.request_id)
            self.send_json(200, chat_completion(final, completion_id, created, requested, legacy), headers)
            return

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        writer = SSEWriter(self, headers)
        gen = self.engine.run(req, resolved, self.request_id)

        def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> None:
            writer.data(chat_chunk(completion_id, created, requested, delta, finish, include_usage))

        try:
            for kind, payload in gen:
                if not writer.started:
                    writer.start()
                    chunk({"role": "assistant", "content": "", "refusal": None})
                if kind == "keepalive":
                    writer.comment("keep-alive")
                elif kind == "text":
                    if payload:
                        chunk({"content": payload})
                elif kind == "final":
                    final: FinalResult = payload
                    finish = final.finish_reason
                    if final.tool_calls and legacy:
                        c = final.tool_calls[0]
                        chunk({"function_call": {"name": c.name, "arguments": c.arguments}})
                        finish = "function_call"
                    else:
                        for i, c in enumerate(final.tool_calls):
                            chunk({"tool_calls": [{"index": i, "id": c.id, "type": "function",
                                                   "function": {"name": c.name, "arguments": ""}}]})
                            chunk({"tool_calls": [{"index": i, "function": {"arguments": c.arguments}}]})
                    chunk({}, finish)
                    if include_usage:
                        writer.data(chat_usage_chunk(completion_id, created, requested, final))
                    writer.data("[DONE]")
            writer.end()
        except BridgeError as err:
            if not writer.started:
                raise
            log.warning("[%s] ストリーミング中にエラー: %s", self.request_id, err.message)
            try:
                writer.data(err.to_dict())
                writer.end()
            except ClientGone:
                pass
        finally:
            gen.close()

    # ------------------------------------------------------------------ /v1/responses
    def handle_get_response(self, response_id: str) -> None:
        stored = self.server.store.get(response_id)
        if not stored:
            raise BridgeError(404, f"Response '{response_id}' が見つかりません（サーバー再起動で消えます）。",
                              "invalid_request_error", code="not_found")
        self.send_json(200, stored[0])

    def handle_responses(self, body: Dict[str, Any]) -> None:
        if body.get("background"):
            raise invalid_request("background モードは未対応です。", param="background")
        history: List[Turn] = []
        prev_id = body.get("previous_response_id")
        if prev_id:
            stored = self.server.store.get(prev_id)
            if not stored:
                raise BridgeError(400, f"Previous response with id '{prev_id}' not found.（このブリッジはメモリ保存のため、"
                                       "サーバー再起動で消えます）", "invalid_request_error",
                                  code="previous_response_not_found", param="previous_response_id")
            history = list(stored[1])
        requested, resolved = self._resolve_model(body)
        req = responses_request_from_body(body, self._ref, history)
        response_id = new_id("resp_", 40)
        message_id = new_id("msg_", 40)
        created = now()
        headers = self._common_headers(resolved)
        store = body.get("store", True) is not False

        def save(final: FinalResult, resp: Dict[str, Any]) -> None:
            if store:
                self.server.store.put(resp, history + new_turns_for_history(body) + [output_turn(final)])

        if not body.get("stream"):
            final = self.engine.complete(req, resolved, self.request_id)
            call_ids = [new_id("fc_", 40) for _ in final.tool_calls]
            output = final_output_items(final, message_id, call_ids)
            resp = response_object(response_id, created, requested, body, "completed", output, final)
            save(final, resp)
            self.send_json(200, resp, headers)
            return

        writer = SSEWriter(self, headers)
        gen = self.engine.run(req, resolved, self.request_id)
        seq = [0]
        text_parts: List[str] = []
        msg_open = [False]

        def emit(etype: str, payload: Dict[str, Any]) -> None:
            obj = {"type": etype, "sequence_number": seq[0]}
            obj.update(payload)
            seq[0] += 1
            writer.data(obj, event=etype)

        def open_message() -> None:
            if msg_open[0]:
                return
            msg_open[0] = True
            emit("response.output_item.added", {"output_index": 0,
                                                "item": response_message_item(message_id, "", "in_progress")})
            emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0,
                                                 "part": output_text_part("")})

        try:
            for kind, payload in gen:
                if not writer.started:
                    writer.start()
                    base = response_object(response_id, created, requested, body, "in_progress", [])
                    emit("response.created", {"response": base})
                    emit("response.in_progress", {"response": base})
                if kind == "keepalive":
                    writer.comment("keep-alive")
                elif kind == "text":
                    if payload:
                        open_message()
                        text_parts.append(payload)
                        emit("response.output_text.delta", {"item_id": message_id, "output_index": 0,
                                                            "content_index": 0, "delta": payload, "logprobs": []})
                elif kind == "final":
                    final = payload
                    text = "".join(text_parts)
                    output: List[Dict[str, Any]] = []
                    if msg_open[0] or not final.tool_calls:
                        open_message()
                        emit("response.output_text.done", {"item_id": message_id, "output_index": 0,
                                                           "content_index": 0, "text": text, "logprobs": []})
                        emit("response.content_part.done", {"item_id": message_id, "output_index": 0,
                                                            "content_index": 0, "part": output_text_part(text)})
                        item = response_message_item(message_id, text)
                        emit("response.output_item.done", {"output_index": 0, "item": item})
                        output.append(item)
                    for call in final.tool_calls:
                        idx = len(output)
                        item_id = new_id("fc_", 40)
                        emit("response.output_item.added", {"output_index": idx,
                                                            "item": function_call_item(item_id, call, "in_progress")})
                        emit("response.function_call_arguments.delta", {"item_id": item_id, "output_index": idx,
                                                                         "delta": call.arguments})
                        emit("response.function_call_arguments.done", {"item_id": item_id, "output_index": idx,
                                                                        "name": call.name, "arguments": call.arguments})
                        item = function_call_item(item_id, call)
                        emit("response.output_item.done", {"output_index": idx, "item": item})
                        output.append(item)
                    final.content = text if msg_open[0] else final.content
                    resp = response_object(response_id, created, requested, body, "completed", output, final)
                    save(final, resp)
                    emit("response.completed", {"response": resp})
            writer.end()
        except BridgeError as err:
            if not writer.started:
                raise
            log.warning("[%s] ストリーミング中にエラー: %s", self.request_id, err.message)
            try:
                failed = response_object(response_id, created, requested, body, "failed", [],
                                         error={"code": err.code or "server_error", "message": err.message})
                emit("response.failed", {"response": failed})
                writer.end()
            except ClientGone:
                pass
        finally:
            gen.close()


def serve(cfg: Config, engine: Optional[Engine] = None) -> BridgeServer:
    return BridgeServer(cfg, engine)
