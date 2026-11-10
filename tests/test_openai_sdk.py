"""公式 OpenAI Python SDK から呼んで、レスポンスが SDK で正しく解釈できることを確認する。

`pip install openai` されていない環境ではスキップされる。
"""

from __future__ import annotations

import json
import os
import unittest

from helpers import RunningServer

try:
    import openai
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    openai = None  # type: ignore[assignment]
    BaseModel = object  # type: ignore[assignment,misc]

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "天気を返す",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}


class Answer(BaseModel):  # type: ignore[misc,valid-type]
    answer: int


@unittest.skipIf(openai is None, "openai パッケージが未インストール")
class OpenAISDKTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.pop("FAKE_CURSOR_LOGGED_OUT", None)
        cls.srv = RunningServer()
        cls.client = openai.OpenAI(base_url=cls.srv.base + "/v1", api_key="dummy", max_retries=0, timeout=60)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.close()

    def test_models_list(self) -> None:
        ids = [m.id for m in self.client.models.list()]
        self.assertIn("default", ids)
        self.assertIn("gpt-5", ids)

    def test_chat(self) -> None:
        r = self.client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hello"}])
        self.assertEqual(r.choices[0].message.content, "Echo: hello")
        self.assertEqual(r.choices[0].finish_reason, "stop")
        self.assertGreater(r.usage.total_tokens, 0)

    def test_chat_stream(self) -> None:
        stream = self.client.chat.completions.create(
            model="gpt-5", messages=[{"role": "user", "content": "streaming"}], stream=True,
            stream_options={"include_usage": True})
        text = ""
        usage = None
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text += chunk.choices[0].delta.content
            if chunk.usage:
                usage = chunk.usage
        self.assertEqual(text, "Echo: streaming")
        self.assertIsNotNone(usage)

    def test_chat_stream_helper(self) -> None:
        with self.client.chat.completions.stream(model="gpt-5", messages=[{"role": "user", "content": "x"}]) as s:
            for _ in s:
                pass
            final = s.get_final_completion()
        self.assertEqual(final.choices[0].message.content, "Echo: x")

    def test_chat_tools(self) -> None:
        r = self.client.chat.completions.create(model="gpt-5", tools=[WEATHER_TOOL],
                                                messages=[{"role": "user", "content": "天気"}])
        call = r.choices[0].message.tool_calls[0]
        self.assertEqual(call.function.name, "get_weather")
        self.assertEqual(json.loads(call.function.arguments), {"city": "Tokyo"})
        r2 = self.client.chat.completions.create(model="gpt-5", tools=[WEATHER_TOOL], messages=[
            {"role": "user", "content": "天気"},
            r.choices[0].message.model_dump(exclude_none=True),
            {"role": "tool", "tool_call_id": call.id, "content": "雪"},
        ])
        self.assertEqual(r2.choices[0].message.content, "Tool said: 雪")

    def test_chat_tools_stream_helper(self) -> None:
        with self.client.chat.completions.stream(model="gpt-5", tools=[WEATHER_TOOL],
                                                 messages=[{"role": "user", "content": "天気"}]) as s:
            final = s.get_final_completion()
        call = final.choices[0].message.tool_calls[0]
        self.assertEqual(call.function.name, "get_weather")
        self.assertEqual(final.choices[0].finish_reason, "tool_calls")

    def test_chat_parse_structured_output(self) -> None:
        r = self.client.chat.completions.parse(model="gpt-5", response_format=Answer,
                                               messages=[{"role": "user", "content": "答えは？"}])
        self.assertEqual(r.choices[0].message.parsed.answer, 42)

    def test_responses(self) -> None:
        r = self.client.responses.create(model="gpt-5", instructions="短く", input="こんにちは")
        self.assertEqual(r.output_text, "[sys:短く] Echo: こんにちは")
        r2 = self.client.responses.create(model="gpt-5", previous_response_id=r.id, input="もう一度")
        self.assertEqual(r2.output_text, "Echo: もう一度")

    def test_responses_stream(self) -> None:
        deltas = []
        final = None
        for ev in self.client.responses.create(model="gpt-5", input="ストリーム", stream=True):
            if ev.type == "response.output_text.delta":
                deltas.append(ev.delta)
            if ev.type == "response.completed":
                final = ev.response
        self.assertEqual("".join(deltas), "Echo: ストリーム")
        self.assertEqual(final.output_text, "Echo: ストリーム")

    def test_responses_stream_helper(self) -> None:
        with self.client.responses.stream(model="gpt-5", input="helper") as s:
            for _ in s:
                pass
            final = s.get_final_response()
        self.assertEqual(final.output_text, "Echo: helper")

    def test_responses_function_call(self) -> None:
        tool = {"type": "function", "name": "get_weather", "description": "天気",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}, "strict": False}
        r = self.client.responses.create(model="gpt-5", tools=[tool], input="天気")
        fc = [o for o in r.output if o.type == "function_call"][0]
        self.assertEqual(fc.name, "get_weather")
        r2 = self.client.responses.create(model="gpt-5", tools=[tool], previous_response_id=r.id, input=[
            {"type": "function_call_output", "call_id": fc.call_id, "output": "霧"}])
        self.assertEqual(r2.output_text, "Tool said: 霧")

    def test_responses_parse(self) -> None:
        r = self.client.responses.parse(model="gpt-5", input="答えは？", text_format=Answer)
        self.assertEqual(r.output_parsed.answer, 42)

    def test_error_mapping(self) -> None:
        os.environ["FAKE_CURSOR_LOGGED_OUT"] = "1"
        try:
            with self.assertRaises(openai.AuthenticationError):
                self.client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "x"}])
        finally:
            os.environ.pop("FAKE_CURSOR_LOGGED_OUT", None)


if __name__ == "__main__":
    unittest.main()
