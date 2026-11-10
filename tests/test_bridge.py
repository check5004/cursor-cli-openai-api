"""偽 Cursor CLI を使ったエンドツーエンドテスト（標準ライブラリの unittest のみで実行可能）。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import time
import unittest

from helpers import RunningServer

from cursor_openai_api.config import resolve_windows_node
from cursor_openai_api.cursor_cli import parse_models_output
from cursor_openai_api.prompt import clean_json_output, parse_tool_calls

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "天気を返す",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}

PNG_1PX = base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                  "1f15c4890000000d49444154789c6360000000000500010d0a2db40000000049454e44ae426082")
).decode()


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class BridgeTestBase(unittest.TestCase):
    overrides: dict = {}

    def setUp(self) -> None:
        os.environ.pop("FAKE_CURSOR_LOGGED_OUT", None)
        self.srv = RunningServer(**self.overrides)

    def tearDown(self) -> None:
        self.srv.close()
        os.environ.pop("FAKE_CURSOR_LOGGED_OUT", None)


class ChatCompletionsTest(BridgeTestBase):
    def test_health(self) -> None:
        status, body, _ = self.srv.json("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_models(self) -> None:
        status, body, _ = self.srv.json("GET", "/v1/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in body["data"]]
        self.assertEqual(ids[0], "default")
        for mid in ("auto", "composer-2", "sonnet-4.5", "gpt-5"):
            self.assertIn(mid, ids)
        self.assertNotIn("Tip", ids)
        status, one, _ = self.srv.json("GET", "/v1/models/sonnet-4.5")
        self.assertEqual(status, 200)
        self.assertEqual(one["display_name"], "Claude 4.5 Sonnet")
        status, _, _ = self.srv.json("GET", "/v1/models/nope")
        self.assertEqual(status, 404)

    def test_path_without_v1_prefix(self) -> None:
        status, body, _ = self.srv.json("POST", "/chat/completions",
                                        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, body)

    def test_basic_completion(self) -> None:
        status, body, hdrs = self.srv.json("POST", "/v1/chat/completions", {
            "model": "sonnet-4.5",
            "messages": [{"role": "user", "content": "こんにちは"}],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "sonnet-4.5")
        msg = body["choices"][0]["message"]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "Echo: こんにちは")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertGreater(body["usage"]["total_tokens"], 0)
        self.assertEqual(hdrs.get("x-cursor-model"), "sonnet-4.5")
        argv = self.srv.prompt_calls()[-1]
        for flag in ("-p", "--output-format", "stream-json", "--stream-partial-output", "--trust", "--mode", "ask"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet-4.5")
        # プロンプトは一時ディレクトリで実行される
        self.assertIn("cursor-openai-api-", self.srv.calls()[-1]["cwd"])

    def test_system_and_history(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto",
            "messages": [
                {"role": "system", "content": "関西弁で"},
                {"role": "user", "content": "1つ目"},
                {"role": "assistant", "content": "はい"},
                {"role": "user", "content": [{"type": "text", "text": "2つ目"}]},
            ],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["choices"][0]["message"]["content"], "[sys:関西弁で] Echo: 2つ目")

    def test_streaming(self) -> None:
        events = self.srv.sse("/v1/chat/completions", {
            "model": "auto", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "stream please"}],
        })
        self.assertEqual(events[-1][1], "[DONE]")
        chunks = [json.loads(d) for _, d in events[:-1]]
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
        self.assertEqual(text, "Echo: stream please")
        finishes = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]]
        self.assertEqual(finishes[-1], "stop")
        self.assertGreater(len([c for c in chunks if c["choices"] and c["choices"][0]["delta"].get("content")]), 2)
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertGreater(chunks[-1]["usage"]["completion_tokens"], 0)

    def test_tool_call(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "tools": [WEATHER_TOOL],
            "messages": [{"role": "user", "content": "東京の天気は？"}],
        })
        self.assertEqual(status, 200, body)
        choice = body["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        call = choice["message"]["tool_calls"][0]
        self.assertTrue(call["id"].startswith("call_"))
        self.assertEqual(call["function"]["name"], "get_weather")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "Tokyo"})

        # ツール結果を返すと、通常の回答になる
        status, body2, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "tools": [WEATHER_TOOL],
            "messages": [
                {"role": "user", "content": "東京の天気は？"},
                choice["message"],
                {"role": "tool", "tool_call_id": call["id"], "content": "晴れ 20℃"},
            ],
        })
        self.assertEqual(status, 200, body2)
        self.assertEqual(body2["choices"][0]["message"]["content"], "Tool said: 晴れ 20℃")
        self.assertEqual(body2["choices"][0]["finish_reason"], "stop")

    def test_tool_call_streaming(self) -> None:
        events = self.srv.sse("/v1/chat/completions", {
            "model": "auto", "stream": True, "tools": [WEATHER_TOOL],
            "messages": [{"role": "user", "content": "東京の天気は？"}],
        })
        chunks = [json.loads(d) for _, d in events[:-1]]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        self.assertFalse(any(d.get("content") for d in deltas))
        tc = [d["tool_calls"][0] for d in deltas if d.get("tool_calls")]
        self.assertEqual(tc[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads("".join(t["function"]["arguments"] for t in tc)), {"city": "Tokyo"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_tool_choice_none_skips_tools(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "tools": [WEATHER_TOOL], "tool_choice": "none",
            "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(body["choices"][0]["message"]["content"], "Echo: hello")

    def test_json_mode(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": "JSON で"}],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"]), {"answer": 42})

    def test_json_schema_streaming(self) -> None:
        events = self.srv.sse("/v1/chat/completions", {
            "model": "auto", "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": {"type": "object", "properties": {"answer": {"type": "integer"}}}}},
            "messages": [{"role": "user", "content": "JSON で"}],
        })
        text = "".join(json.loads(d)["choices"][0]["delta"].get("content") or "" for _, d in events[:-1])
        self.assertEqual(json.loads(text), {"answer": 42})

    def test_image_attachment(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "IMAGE_CHECK この画像は？"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_1PX}},
            ]}],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["choices"][0]["message"]["content"], "IMAGE_OK")

    def test_n_greater_than_one_is_rejected(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "n": 2, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["param"], "n")

    def test_invalid_body(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {"model": "auto"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_cli_failure_is_not_retryable(self) -> None:
        status, body, hdrs = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "FAKE_FAIL"}]})
        self.assertEqual(status, 500)
        self.assertIn("something went wrong", body["error"]["message"])
        self.assertEqual(hdrs.get("x-should-retry"), "false")

    def test_not_logged_in(self) -> None:
        os.environ["FAKE_CURSOR_LOGGED_OUT"] = "1"
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "default", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "cursor_not_authenticated")

    def test_invalid_model_from_cli(self) -> None:
        self.srv.cfg.extra_models = ["bad-model"]
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "bad-model", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "model_not_found")

    def test_unknown_model_falls_back_to_default(self) -> None:
        status, body, hdrs = self.srv.json("POST", "/v1/chat/completions", {
            "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["model"], "gpt-4o-mini")  # 応答の model はリクエストした名前のまま
        self.assertNotIn("--model", self.srv.prompt_calls()[-1])


class ModelOptionsTest(BridgeTestBase):
    overrides = {"model_map": {"gpt-4o": "sonnet-4.5"}, "default_model": "composer-2", "strict_models": True}

    def test_model_map(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, body)
        argv = self.srv.prompt_calls()[-1]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet-4.5")
        ids = [m["id"] for m in self.srv.json("GET", "/v1/models")[1]["data"]]
        self.assertIn("gpt-4o", ids)

    def test_default_model(self) -> None:
        self.srv.json("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        argv = self.srv.prompt_calls()[-1]
        self.assertEqual(argv[argv.index("--model") + 1], "composer-2")

    def test_strict_models(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "model_not_found")


class AuthTest(BridgeTestBase):
    overrides = {"api_key": "secret-key"}

    def test_requires_key(self) -> None:
        status, body, _ = self.srv.json("GET", "/v1/models")
        self.assertEqual(status, 401)
        status, body, _ = self.srv.json("GET", "/v1/models", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, body, _ = self.srv.json("GET", "/v1/models", headers={"Authorization": "Bearer secret-key"})
        self.assertEqual(status, 200)
        status, _, _ = self.srv.json("GET", "/health")
        self.assertEqual(status, 200)


class FileTransportTest(BridgeTestBase):
    overrides = {"prompt_transport": "file"}

    def test_file_transport_hides_preamble(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "長いプロンプトの代わり"}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["choices"][0]["message"]["content"], "Echo: 長いプロンプトの代わり")

    def test_file_transport_streaming(self) -> None:
        events = self.srv.sse("/v1/chat/completions", {
            "model": "auto", "stream": True, "messages": [{"role": "user", "content": "abc"}]})
        text = "".join(json.loads(d)["choices"][0]["delta"].get("content") or "" for _, d in events[:-1])
        self.assertEqual(text, "Echo: abc")


class AutoTransportTest(BridgeTestBase):
    overrides = {"max_arg_bytes": 2000}

    def test_large_prompt_goes_through_file(self) -> None:
        big = "あ" * 1000
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": big}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["choices"][0]["message"]["content"], "Echo: " + big)
        self.assertTrue(self.srv.prompt_calls()[-1][-1].startswith("Read the file ./prompt.md"))
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "short"}]})
        self.assertEqual(self.srv.prompt_calls()[-1][-1].count("short"), 1)


class SharedWorkspaceTest(BridgeTestBase):
    def setUp(self) -> None:
        self.ws = tempfile.mkdtemp(prefix="bridge-ws-")
        self.overrides = {"workspace": self.ws}
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_attachments_in_shared_workspace_are_cleaned(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "IMAGE_CHECK"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_1PX}},
            ]}],
        })
        self.assertEqual(body["choices"][0]["message"]["content"], "IMAGE_OK")
        self.assertEqual(os.path.realpath(self.srv.calls()[-1]["cwd"]), os.path.realpath(self.ws))
        leftovers = os.listdir(os.path.join(self.ws, ".cursor-openai-api"))
        self.assertEqual(leftovers, [])


@unittest.skipIf(os.name == "nt", "プロセス生存確認に os.kill(pid, 0) を使うため POSIX のみ")
class DisconnectTest(BridgeTestBase):
    def test_client_disconnect_kills_cli(self) -> None:
        pidfile = tempfile.mktemp(prefix="fake-pid-")
        os.environ["FAKE_CURSOR_PIDFILE"] = pidfile
        try:
            body = json.dumps({"model": "auto", "stream": True,
                               "messages": [{"role": "user", "content": "FAKE_SLEEP"}]}).encode()
            sock = socket.create_connection(("127.0.0.1", self.srv.port))
            sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            for _ in range(100):
                if os.path.exists(pidfile) and _read(pidfile).strip():
                    break
                time.sleep(0.05)
            pid = int(_read(pidfile))
            sock.close()
            # キープアライブ送信時に切断を検知して CLI を止める（最大 KEEPALIVE_SECONDS + α）
            deadline = time.time() + 25
            alive = True
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                    try:
                        if os.waitpid(pid, os.WNOHANG) != (0, 0):
                            alive = False
                            break
                    except ChildProcessError:
                        pass
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.2)
            self.assertFalse(alive, "クライアント切断後も Cursor CLI が動き続けている")
        finally:
            os.environ.pop("FAKE_CURSOR_PIDFILE", None)


class WindowsLauncherTest(unittest.TestCase):
    def test_resolve_latest_version(self) -> None:
        base = tempfile.mkdtemp(prefix="cursor-agent-")
        try:
            for ver in ("2026.9.1-aaa111", "2026.11.05-86047df", "2026.10.30-bbb222", "not-a-version"):
                d = os.path.join(base, "versions", ver)
                os.makedirs(d)
                for f in ("node.exe", "index.js"):
                    open(os.path.join(d, f), "w").close()
            cmd = resolve_windows_node(base)
            self.assertEqual(cmd, [os.path.join(base, "versions", "2026.11.05-86047df", "node.exe"),
                                   os.path.join(base, "versions", "2026.11.05-86047df", "index.js")])
            self.assertIsNone(resolve_windows_node(os.path.join(base, "missing")))
        finally:
            shutil.rmtree(base, ignore_errors=True)


class NoPartialTest(BridgeTestBase):
    overrides = {"stream_partial": False}

    def test_without_partial_output(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "whole"}]})
        self.assertEqual(body["choices"][0]["message"]["content"], "Echo: whole")
        self.assertNotIn("--stream-partial-output", self.srv.prompt_calls()[-1])


class TimeoutTest(BridgeTestBase):
    overrides = {"timeout": 2}

    def test_timeout(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/chat/completions", {
            "model": "auto", "messages": [{"role": "user", "content": "FAKE_SLEEP"}]})
        self.assertEqual(status, 504)
        self.assertEqual(body["error"]["code"], "timeout")


class ResponsesApiTest(BridgeTestBase):
    def test_basic(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/responses", {
            "model": "auto", "instructions": "丁寧に", "input": "こんにちは"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["status"], "completed")
        msg = body["output"][0]
        self.assertEqual(msg["type"], "message")
        self.assertEqual(msg["content"][0]["text"], "[sys:丁寧に] Echo: こんにちは")
        self.assertGreater(body["usage"]["total_tokens"], 0)

        # previous_response_id で会話を続けられる（instructions は引き継がない）
        status, body2, _ = self.srv.json("POST", "/v1/responses", {
            "model": "auto", "previous_response_id": body["id"], "input": "続き"})
        self.assertEqual(status, 200, body2)
        self.assertEqual(body2["output"][0]["content"][0]["text"], "Echo: 続き")
        status, got, _ = self.srv.json("GET", f"/v1/responses/{body['id']}")
        self.assertEqual(got["id"], body["id"])

    def test_unknown_previous_response(self) -> None:
        status, body, _ = self.srv.json("POST", "/v1/responses", {
            "model": "auto", "previous_response_id": "resp_nope", "input": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "previous_response_not_found")

    def test_streaming(self) -> None:
        events = self.srv.sse("/v1/responses", {"model": "auto", "input": "stream", "stream": True})
        types = [e for e, _ in events]
        self.assertEqual(types[0], "response.created")
        self.assertEqual(types[-1], "response.completed")
        datas = [json.loads(d) for _, d in events]
        self.assertEqual([d["sequence_number"] for d in datas], list(range(len(datas))))
        text = "".join(d["delta"] for d in datas if d["type"] == "response.output_text.delta")
        self.assertEqual(text, "Echo: stream")
        self.assertEqual(datas[-1]["response"]["output"][0]["content"][0]["text"], "Echo: stream")

    def test_function_call(self) -> None:
        tool = {"type": "function", "name": "get_weather", "description": "天気",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}
        status, body, _ = self.srv.json("POST", "/v1/responses", {
            "model": "auto", "tools": [tool], "input": "東京の天気"})
        self.assertEqual(status, 200, body)
        fc = body["output"][0]
        self.assertEqual(fc["type"], "function_call")
        self.assertEqual(fc["name"], "get_weather")
        self.assertEqual(json.loads(fc["arguments"]), {"city": "Tokyo"})

        status, body2, _ = self.srv.json("POST", "/v1/responses", {
            "model": "auto", "tools": [tool], "previous_response_id": body["id"],
            "input": [{"type": "function_call_output", "call_id": fc["call_id"], "output": "くもり"}]})
        self.assertEqual(status, 200, body2)
        self.assertEqual(body2["output"][0]["content"][0]["text"], "Tool said: くもり")

    def test_function_call_streaming(self) -> None:
        tool = {"type": "function", "name": "get_weather", "parameters": {"type": "object", "properties": {}}}
        events = self.srv.sse("/v1/responses", {"model": "auto", "tools": [tool], "input": "x", "stream": True})
        types = [e for e, _ in events]
        self.assertIn("response.function_call_arguments.done", types)
        self.assertNotIn("response.output_text.delta", types)
        final = json.loads(events[-1][1])["response"]
        self.assertEqual(final["output"][0]["type"], "function_call")


class UnitTests(unittest.TestCase):
    def test_parse_models_variants(self) -> None:
        self.assertEqual([m["id"] for m in parse_models_output(
            "Available models\n\nauto - Auto\ngpt-5 - GPT-5 (current)\n\nTip: use --model <id> to switch.\n")],
            ["auto", "gpt-5"])
        self.assertEqual([m["id"] for m in parse_models_output('[{"id": "a"}, {"id": "b"}]')], ["a", "b"])
        self.assertEqual([m["id"] for m in parse_models_output("Models:\n  sonnet-4.5\n  gpt-5 (default)\n")],
                         ["sonnet-4.5", "gpt-5"])

    def test_parse_tool_calls(self) -> None:
        rest, calls = parse_tool_calls('{"tool_calls":[{"name":"f","arguments":{"a":1}}]}', ["f"])
        self.assertEqual(calls[0].name, "f")
        self.assertEqual(json.loads(calls[0].arguments), {"a": 1})
        self.assertEqual(rest, "")
        rest, calls = parse_tool_calls('考えます\n{"tool_calls":[{"name":"f","arguments":"{\\"a\\":2}"}]}', ["f"])
        self.assertEqual(rest, "考えます")
        self.assertEqual(json.loads(calls[0].arguments), {"a": 2})
        self.assertIsNone(parse_tool_calls('{"tool_calls":[{"name":"unknown","arguments":{}}]}', ["f"])[1])
        self.assertIsNone(parse_tool_calls("普通の回答です", ["f"])[1])
        self.assertIsNone(parse_tool_calls('{"name": "f"}', ["f"])[1])

    def test_clean_json_output(self) -> None:
        self.assertEqual(clean_json_output('```json\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(clean_json_output('結果:\n{"a": 1}\n以上'), '{"a": 1}')
        self.assertEqual(clean_json_output('[1, 2]'), '[1, 2]')


if __name__ == "__main__":
    unittest.main()
