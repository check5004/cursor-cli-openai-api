#!/usr/bin/env python3
"""テスト用の偽 Cursor CLI。本物の `agent -p --output-format stream-json` の出力形式を模倣する。

環境変数:
  FAKE_CURSOR_LOG        … 受け取った argv を JSON Lines で追記するファイル
  FAKE_CURSOR_LOGGED_OUT … 1 なら未ログインとして失敗する
  FAKE_CURSOR_PIDFILE    … 自分の PID を書き出すファイル（中断テスト用）
"""

import json
import os
import re
import sys
import time
import uuid

MODELS_TEXT = """\x1b[1mAvailable models\x1b[0m

auto - Auto
composer-2 - Composer 2  (current)
sonnet-4.5 - Claude 4.5 Sonnet
gpt-5 - GPT-5 (default)

Tip: use --model <id> (or /model <id> in interactive mode) to switch.
"""


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def last_block(prompt, tag):
    blocks = re.findall(r"<%s(?:\s[^>]*)?>\n(.*?)\n</%s>" % (tag, tag), prompt, re.S)
    return blocks[-1] if blocks else None


def decide_answer(prompt):
    if "FAKE_SLEEP" in prompt:
        time.sleep(30)
    if "FAKE_FAIL" in prompt:
        sys.stderr.write("Error: something went wrong inside the agent\n")
        sys.exit(2)
    if "IMAGE_CHECK" in prompt:
        paths = re.findall(r"\[Attached image: (\S+)", prompt)
        ok = paths and all(os.path.isfile(p) for p in paths)
        return "IMAGE_OK" if ok else "IMAGE_MISSING"
    has_result = re.search(r"<tool_result[^>]*>\n", prompt) is not None
    if "<available_functions>" in prompt and not has_result:
        specs = json.loads(prompt.split("<available_functions>\n", 1)[1].split("\n</available_functions>", 1)[0])
        name = specs[0]["name"]
        forced = re.search(r'MUST call the function "([^"]+)"', prompt)
        if forced:
            name = forced.group(1)
        if "FAKE_PROSE_TOOL" in prompt:
            return 'Let me check.\n{"tool_calls": [{"name": "%s", "arguments": {"city": "Tokyo"}}]}' % name
        return '```json\n{"tool_calls": [{"name": "%s", "arguments": {"city": "Tokyo"}}]}\n```' % name
    if has_result:
        result = last_block(prompt, "tool_result")
        return "Tool said: %s" % (result or "").strip()
    if "Output format: respond with a single valid JSON" in prompt:
        return 'Here you go:\n```json\n{"answer": 42}\n```'
    user = last_block(prompt, "user") or last_block(prompt, "user_message") or prompt
    system = last_block(prompt, "system_instructions")
    prefix = "[sys:%s] " % system.strip() if system else ""
    return prefix + "Echo: " + user.strip()


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows でも本物の CLI と同じく UTF-8 で出力する
    if not argv or argv[0] in ("--version", "-v"):
        print("2026.11.05-fake")
        return 0
    if os.environ.get("FAKE_CURSOR_LOG"):
        with open(os.environ["FAKE_CURSOR_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv[0] == "status":
        if os.environ.get("FAKE_CURSOR_LOGGED_OUT") == "1":
            print("Not logged in")
            return 1
        print("\x1b[32m✓\x1b[0m Logged in as test@example.com")
        return 0
    if argv[0] in ("models", "--list-models"):
        sys.stdout.write(MODELS_TEXT)
        return 0
    if "-p" not in argv and "--print" not in argv:
        sys.stderr.write("interactive mode is not supported by the fake\n")
        return 1
    if os.environ.get("FAKE_CURSOR_PIDFILE"):
        with open(os.environ["FAKE_CURSOR_PIDFILE"], "w") as fh:
            fh.write(str(os.getpid()))

    opts = {"partial": False, "model": None, "format": "text"}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--output-format", "--model", "--mode", "--workspace", "--sandbox"):
            if a == "--output-format":
                opts["format"] = argv[i + 1]
            if a == "--model":
                opts["model"] = argv[i + 1]
            i += 2
            continue
        if a == "--stream-partial-output":
            opts["partial"] = True
        elif a.startswith("-"):
            pass
        else:
            positional.append(a)
        i += 1
    prompt = positional[-1] if positional else ""

    if os.environ.get("FAKE_CURSOR_LOGGED_OUT") == "1":
        sys.stderr.write("Error: Not authenticated. Please run `agent login` first.\n")
        return 1
    if opts["model"] == "bad-model":
        sys.stderr.write("Error: Model name is not valid: bad-model\n")
        return 1

    sid = str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": os.getcwd(),
          "session_id": sid, "model": opts["model"] or "Composer 2", "permissionMode": "default"})
    emit({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
          "session_id": sid})

    pieces = []
    m = re.match(r"Read the file (\S+) in the current directory", prompt)
    if m:
        pre = "I'll read the request file first."
        if opts["partial"]:
            emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": pre}]},
                  "session_id": sid, "timestamp_ms": int(time.time() * 1000)})
            emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": pre}]},
                  "session_id": sid, "timestamp_ms": int(time.time() * 1000), "model_call_id": "mc1"})
        else:
            emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": pre}]},
                  "session_id": sid})
        pieces.append(pre)
        path = m.group(1)
        emit({"type": "tool_call", "subtype": "started", "call_id": "t1",
              "tool_call": {"readToolCall": {"args": {"path": path}}}, "session_id": sid})
        with open(path, encoding="utf-8") as fh:
            prompt = fh.read()
        emit({"type": "tool_call", "subtype": "completed", "call_id": "t1",
              "tool_call": {"readToolCall": {"args": {"path": path}, "result": {"success": {"content": "..."}}}},
              "session_id": sid})

    answer = decide_answer(prompt)
    pieces.append(answer)
    if opts["format"] == "stream-json":
        if opts["partial"]:
            for k in range(0, len(answer), 5):
                emit({"type": "assistant",
                      "message": {"role": "assistant", "content": [{"type": "text", "text": answer[k:k + 5]}]},
                      "session_id": sid, "timestamp_ms": int(time.time() * 1000)})
                time.sleep(0.005)
        # 部分出力ありでもなしでも、ターン終了時にはメッセージ全体が 1 回送られる
        emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]},
              "session_id": sid})
        emit({"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 10, "is_error": False,
              "result": "".join(pieces), "session_id": sid, "request_id": str(uuid.uuid4())})
    elif opts["format"] == "json":
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "".join(pieces), "session_id": sid})
    else:
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
