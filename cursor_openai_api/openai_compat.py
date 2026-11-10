"""OpenAI API のリクエスト/レスポンス形式との相互変換。"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from .errors import invalid_request
from .prompt import (BridgeRequest, ContentConverter, ResponseFormat, ToolCall, ToolChoice, ToolSpec, Turn,
                     new_id)
from .engine import FinalResult

# ============================================================================ 共通

def _parse_tool_choice(value: Any, param: str) -> ToolChoice:
    if value is None:
        return "auto"
    if isinstance(value, str):
        if value in ("auto", "none", "required"):
            return value
        raise invalid_request(f"{param} は auto / none / required のいずれかを指定してください。", param=param)
    if isinstance(value, dict):
        fn = value.get("function") if isinstance(value.get("function"), dict) else value
        name = fn.get("name")
        if isinstance(name, str) and name:
            return {"name": name}
        if value.get("type") in ("allowed_tools",):
            mode = value.get("mode", "auto")
            return "required" if mode == "required" else "auto"
    raise invalid_request(f"{param} の形式が不正です。", param=param)


def _arguments_to_str(args: Any) -> str:
    if isinstance(args, str):
        return args
    return json.dumps(args if args is not None else {}, ensure_ascii=False)


# ============================================================================ Chat Completions

def chat_request_from_body(body: Dict[str, Any], ref: Callable[[str], str]) -> BridgeRequest:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid_request("messages は 1 件以上の配列で指定してください。", param="messages")
    conv = ContentConverter(ref)
    turns: List[Turn] = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise invalid_request(f"messages[{i}] はオブジェクトで指定してください。", param=f"messages[{i}]")
        role = m.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool", "function"):
            raise invalid_request(f"messages[{i}].role が不正です: {role!r}", param=f"messages[{i}].role")
        text = conv.to_text(m.get("content"))
        if role == "assistant":
            calls = []
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                calls.append(ToolCall(id=c.get("id") or new_id("call_"), name=fn.get("name", ""),
                                      arguments=_arguments_to_str(fn.get("arguments"))))
            if m.get("function_call"):
                fc = m["function_call"]
                calls.append(ToolCall(id=new_id("call_"), name=fc.get("name", ""),
                                      arguments=_arguments_to_str(fc.get("arguments"))))
            turns.append(Turn("assistant", text, tool_calls=calls))
        elif role in ("tool", "function"):
            turns.append(Turn("tool", text, tool_call_id=m.get("tool_call_id"), name=m.get("name")))
        else:
            turns.append(Turn(role, text))

    tools: List[ToolSpec] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("type", "function") != "function":
            continue
        fn = t.get("function") or {}
        if fn.get("name"):
            tools.append(ToolSpec(fn["name"], fn.get("description") or "", fn.get("parameters")))
    for fn in body.get("functions") or []:  # 旧 function calling
        if isinstance(fn, dict) and fn.get("name"):
            tools.append(ToolSpec(fn["name"], fn.get("description") or "", fn.get("parameters")))

    tool_choice = _parse_tool_choice(body.get("tool_choice", body.get("function_call")), "tool_choice")
    rf = ResponseFormat()
    raw_rf = body.get("response_format")
    if isinstance(raw_rf, dict):
        kind = raw_rf.get("type", "text")
        if kind == "json_object":
            rf = ResponseFormat("json_object")
        elif kind == "json_schema":
            js = raw_rf.get("json_schema") or {}
            rf = ResponseFormat("json_schema", name=js.get("name"), schema=js.get("schema"))
    return BridgeRequest(
        turns=turns,
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=body.get("parallel_tool_calls", True) is not False,
        response_format=rf,
        attachments=conv.attachments,
    )


def _usage(final: FinalResult) -> Dict[str, Any]:
    return {
        "prompt_tokens": final.prompt_tokens,
        "completion_tokens": final.completion_tokens,
        "total_tokens": final.total_tokens,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0,
                                      "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0},
    }


def chat_tool_calls(calls: List[ToolCall]) -> List[Dict[str, Any]]:
    return [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]


def chat_completion(final: FinalResult, completion_id: str, created: int, model: str,
                    legacy_function_call: bool = False) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": final.content if (final.content or not final.tool_calls) else None,
                               "refusal": None, "annotations": []}
    finish = final.finish_reason
    if final.tool_calls:
        if legacy_function_call:
            c = final.tool_calls[0]
            message["function_call"] = {"name": c.name, "arguments": c.arguments}
            finish = "function_call"
        else:
            message["tool_calls"] = chat_tool_calls(final.tool_calls)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish}],
        "usage": _usage(final),
        "service_tier": "default",
        "system_fingerprint": None,
    }


def chat_chunk(completion_id: str, created: int, model: str, delta: Dict[str, Any],
               finish_reason: Optional[str] = None, include_usage: bool = False) -> Dict[str, Any]:
    chunk: Dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "service_tier": "default",
        "system_fingerprint": None,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
    }
    if include_usage:
        chunk["usage"] = None
    return chunk


def chat_usage_chunk(completion_id: str, created: int, model: str, final: FinalResult) -> Dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "service_tier": "default",
        "system_fingerprint": None,
        "choices": [],
        "usage": _usage(final),
    }


# ============================================================================ Responses API

def responses_turns_from_input(body: Dict[str, Any], conv: ContentConverter) -> List[Turn]:
    raw = body.get("input")
    turns: List[Turn] = []
    if raw is None:
        return turns
    if isinstance(raw, str):
        return [Turn("user", raw)]
    if not isinstance(raw, list):
        raise invalid_request("input は文字列または配列で指定してください。", param="input")
    for i, item in enumerate(raw):
        if isinstance(item, str):
            turns.append(Turn("user", item))
            continue
        if not isinstance(item, dict):
            raise invalid_request(f"input[{i}] の形式が不正です。", param=f"input[{i}]")
        itype = item.get("type")
        if itype in (None, "message") and "role" in item:
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise invalid_request(f"input[{i}].role が不正です: {role!r}", param=f"input[{i}].role")
            turns.append(Turn(role, conv.to_text(item.get("content"))))
        elif itype == "function_call":
            turns.append(Turn("assistant", "", tool_calls=[ToolCall(
                id=item.get("call_id") or item.get("id") or new_id("call_"),
                name=item.get("name", ""),
                arguments=_arguments_to_str(item.get("arguments")),
            )]))
        elif itype == "function_call_output":
            output = item.get("output")
            text = output if isinstance(output, str) else conv.to_text(output)
            turns.append(Turn("tool", text, tool_call_id=item.get("call_id")))
        elif itype in ("reasoning", "item_reference"):
            continue
        elif itype in ("input_text", "input_image", "input_file"):
            turns.append(Turn("user", conv.to_text([item])))
        else:
            # 未対応のアイテム（web_search_call など）は文字列化して会話に残す
            turns.append(Turn("user", json.dumps(item, ensure_ascii=False)))
    return turns


def responses_request_from_body(body: Dict[str, Any], ref: Callable[[str], str],
                                history: Optional[List[Turn]] = None) -> BridgeRequest:
    conv = ContentConverter(ref)
    turns: List[Turn] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        turns.append(Turn("system", instructions))
    turns.extend(history or [])
    new_turns = responses_turns_from_input(body, conv)
    if not new_turns and not history:
        raise invalid_request("input を指定してください。", param="input")
    turns.extend(new_turns)

    tools: List[ToolSpec] = []
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "function" and t.get("name"):
            tools.append(ToolSpec(t["name"], t.get("description") or "", t.get("parameters")))

    rf = ResponseFormat()
    fmt = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_object":
            rf = ResponseFormat("json_object")
        elif fmt.get("type") == "json_schema":
            rf = ResponseFormat("json_schema", name=fmt.get("name"), schema=fmt.get("schema"))
    return BridgeRequest(
        turns=turns,
        tools=tools,
        tool_choice=_parse_tool_choice(body.get("tool_choice"), "tool_choice"),
        parallel_tool_calls=body.get("parallel_tool_calls", True) is not False,
        response_format=rf,
        attachments=conv.attachments,
    )


def new_turns_for_history(body: Dict[str, Any]) -> List[Turn]:
    """previous_response_id 用に、今回の input を（添付なしの）テキストとして保存する。"""
    conv = ContentConverter(lambda name: f"(attachment {name} from an earlier turn, no longer available)")
    return responses_turns_from_input(body, conv)


def output_turn(final: FinalResult) -> Turn:
    return Turn("assistant", final.content, tool_calls=list(final.tool_calls))


def response_message_item(item_id: str, text: str, status: str = "completed") -> Dict[str, Any]:
    content = [] if status == "in_progress" else [output_text_part(text)]
    return {"id": item_id, "type": "message", "status": status, "role": "assistant", "content": content}


def output_text_part(text: str) -> Dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": [], "logprobs": []}


def function_call_item(item_id: str, call: ToolCall, status: str = "completed") -> Dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "status": status,
        "call_id": call.id,
        "name": call.name,
        "arguments": call.arguments if status == "completed" else "",
    }


def response_object(response_id: str, created: int, model: str, body: Dict[str, Any], status: str,
                    output: List[Dict[str, Any]], final: Optional[FinalResult] = None,
                    error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    text_cfg = body.get("text") if isinstance(body.get("text"), dict) else {"format": {"type": "text"}}
    tools_echo = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    usage = None
    if final is not None:
        usage = {
            "input_tokens": final.prompt_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": final.completion_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": final.total_tokens,
        }
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "background": False,
        "error": error,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "max_tool_calls": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": body.get("parallel_tool_calls", True) is not False,
        "previous_response_id": body.get("previous_response_id"),
        "reasoning": {"effort": None, "summary": None},
        "service_tier": "default",
        "store": body.get("store", True) is not False,
        "temperature": body.get("temperature", 1.0),
        "text": text_cfg,
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": tools_echo,
        "top_p": body.get("top_p", 1.0),
        "truncation": body.get("truncation", "disabled"),
        "usage": usage,
        "user": body.get("user"),
        "metadata": body.get("metadata") or {},
    }


def final_output_items(final: FinalResult, message_id: str, call_item_ids: List[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if final.content or not final.tool_calls:
        items.append(response_message_item(message_id, final.content))
    for item_id, call in zip(call_item_ids, final.tool_calls):
        items.append(function_call_item(item_id, call))
    return items


def now() -> int:
    return int(time.time())
