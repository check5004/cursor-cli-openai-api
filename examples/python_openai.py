"""公式 OpenAI Python SDK から使う例。

    pip install openai
    python examples/python_openai.py

アプリ側のコードを変えずに切り替えたい場合は、環境変数だけで向き先を変えられます。
    OPENAI_BASE_URL=http://localhost:8765/v1  OPENAI_API_KEY=dummy
"""

import json
import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8765/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "dummy"),  # ブリッジ側で BRIDGE_API_KEY を設定した場合はその値
)
MODEL = os.environ.get("MODEL", "default")

# 1. 普通の呼び出し
res = client.chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": "あなたは簡潔に答えるアシスタントです。"},
        {"role": "user", "content": "富士山の高さは？"},
    ],
)
print("chat:", res.choices[0].message.content)

# 2. ストリーミング
print("stream: ", end="", flush=True)
for chunk in client.chat.completions.create(
    model=MODEL, stream=True, messages=[{"role": "user", "content": "春の季語を3つ挙げて"}]
):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()

# 3. Function calling（プロンプトで模擬しています）
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "指定した都市の現在の天気を返す",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}]
messages = [{"role": "user", "content": "大阪の天気を調べて教えて"}]
res = client.chat.completions.create(model=MODEL, messages=messages, tools=tools)
msg = res.choices[0].message
if msg.tool_calls:
    call = msg.tool_calls[0]
    print("tool call:", call.function.name, call.function.arguments)
    messages.append(msg.model_dump(exclude_none=True))
    messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"weather": "晴れ", "temp": 21}, ensure_ascii=False)})
    res = client.chat.completions.create(model=MODEL, messages=messages, tools=tools)
print("final:", res.choices[0].message.content)

# 4. Responses API
r = client.responses.create(model=MODEL, instructions="一文で答えて", input="TCPとUDPの違いは？")
print("responses:", r.output_text)
