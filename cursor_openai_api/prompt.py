"""OpenAI 形式のメッセージ列を、Cursor CLI に渡す 1 本のプロンプトへ変換する。

Cursor CLI は 1 回の呼び出しが独立した会話なので、毎回これまでの会話全体を文字列化して渡す。
Function calling と JSON モードはプロンプトで指示し、応答を後処理して OpenAI 形式に戻す。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .cursor_cli import Attachment
from .errors import invalid_request

PREAMBLE = """You are being called through an OpenAI-compatible chat completion API by a program, not by a person in an IDE.
Act as a plain chat model and write only the assistant's next reply to the conversation below.
- Do not use tools, do not read or modify files, and do not run commands.{attachment_note}
- Do not mention Cursor, the workspace, or these API instructions.
- Follow the system instructions, if any, and answer in the language the conversation asks for."""

ATTACHMENT_NOTE = " The only exception: you may read the attached files referenced in the conversation."


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    parameters: Optional[Dict[str, Any]] = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 文字列


@dataclass
class Turn:
    role: str  # system | developer | user | assistant | tool
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class ResponseFormat:
    kind: str = "text"  # text | json_object | json_schema
    name: Optional[str] = None
    schema: Optional[Dict[str, Any]] = None


ToolChoice = Union[str, Dict[str, str]]  # "auto" | "none" | "required" | {"name": ...}


@dataclass
class BridgeRequest:
    turns: List[Turn]
    tools: List[ToolSpec] = field(default_factory=list)
    tool_choice: ToolChoice = "auto"
    parallel_tool_calls: bool = True
    response_format: ResponseFormat = field(default_factory=ResponseFormat)
    attachments: List[Attachment] = field(default_factory=list)

    @property
    def tools_active(self) -> bool:
        return bool(self.tools) and self.tool_choice != "none"


def new_id(prefix: str, length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------- 入力の正規化

_DATA_URL_RE = re.compile(r"^data:([\w/+.\-]+)?(;[\w=\-]+)*;base64,(.*)$", re.S)
_EXT_BY_MIME = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/bmp": "bmp", "image/svg+xml": "svg",
    "application/pdf": "pdf", "text/plain": "txt", "text/markdown": "md", "text/csv": "csv",
    "application/json": "json",
}


class ContentConverter:
    """content（文字列またはパーツ配列）をテキスト化し、画像などは添付ファイルとして保存する。"""

    def __init__(self, ref: Callable[[str], str]) -> None:
        self.ref = ref
        self.attachments: List[Attachment] = []

    def _save(self, data: bytes, mime: Optional[str], filename: Optional[str], kind: str) -> str:
        n = len(self.attachments) + 1
        if filename:
            safe = re.sub(r"[^A-Za-z0-9._\-]", "_", filename)[-80:] or f"{kind}_{n}"
            name = f"{n}_{safe}"
        else:
            name = f"{kind}_{n}.{_EXT_BY_MIME.get((mime or '').lower(), 'bin')}"
        self.attachments.append(Attachment(name, data))
        return self.ref(name)

    def _decode_data_url(self, url: str) -> Optional[Tuple[bytes, Optional[str]]]:
        m = _DATA_URL_RE.match(url.strip())
        if not m:
            return None
        try:
            return base64.b64decode(m.group(3), validate=False), m.group(1)
        except (binascii.Error, ValueError):
            raise invalid_request("data URL の base64 をデコードできませんでした。")

    def image(self, url: str) -> str:
        decoded = self._decode_data_url(url) if url.startswith("data:") else None
        if decoded:
            path = self._save(decoded[0], decoded[1], None, "image")
            return f"[Attached image: {path} (read this file to see the image)]"
        return f"[Image URL: {url}]"

    def file(self, part: Dict[str, Any]) -> str:
        info = part.get("file") if isinstance(part.get("file"), dict) else part
        filename = info.get("filename")
        data = info.get("file_data")
        if isinstance(data, str) and data:
            decoded = self._decode_data_url(data) if data.startswith("data:") else None
            if decoded is None:
                try:
                    decoded = (base64.b64decode(data), None)
                except (binascii.Error, ValueError):
                    decoded = (data.encode("utf-8"), "text/plain")
            path = self._save(decoded[0], decoded[1], filename, "file")
            return f"[Attached file: {path}]"
        if info.get("file_url"):
            return f"[File URL: {info['file_url']}]"
        if info.get("file_id"):
            return f"[File id {info['file_id']} (not available in this API)]"
        return "[File omitted]"

    def to_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            return str(content)
        out: List[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "text")
            if ptype in ("text", "input_text", "output_text"):
                out.append(str(part.get("text", "")))
            elif ptype == "refusal":
                out.append(str(part.get("refusal", "")))
            elif ptype == "image_url":
                iu = part.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                out.append(self.image(str(url or "")))
            elif ptype == "input_image":
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                out.append(self.image(str(url)) if url else f"[Image file id {part.get('file_id')} (not available in this API)]")
            elif ptype in ("file", "input_file"):
                out.append(self.file(part))
            elif ptype in ("input_audio", "audio"):
                out.append("[Audio input omitted: not supported]")
            else:
                out.append(json.dumps(part, ensure_ascii=False))
        # テキスト同士は改行なしで連結するのが OpenAI の挙動に近いが、添付参照は読みやすさのため改行で区切る
        joined = ""
        for piece in out:
            if joined and (piece.startswith("[Attached") or piece.startswith("[Image") or joined.endswith("]")):
                joined += "\n"
            joined += piece
        return joined


# ---------------------------------------------------------------------------- プロンプト生成

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _render_tool_calls(calls: List[ToolCall]) -> str:
    items = []
    for c in calls:
        try:
            args: Any = json.loads(c.arguments) if c.arguments else {}
        except ValueError:
            args = c.arguments
        items.append({"id": c.id, "name": c.name, "arguments": args})
    return "<function_calls>\n" + _json({"tool_calls": items}) + "\n</function_calls>"


def render_prompt(req: BridgeRequest, preamble: bool = True) -> str:
    instructions = [t.text for t in req.turns if t.role in ("system", "developer") and t.text.strip()]
    convo = [t for t in req.turns if t.role not in ("system", "developer")]
    tools_active = req.tools_active
    rf = req.response_format

    simple = (not instructions and not tools_active and rf.kind == "text"
              and len(convo) == 1 and convo[0].role == "user")
    if simple and not preamble:
        return convo[0].text

    blocks: List[str] = []
    if preamble:
        blocks.append(PREAMBLE.format(attachment_note=ATTACHMENT_NOTE if req.attachments else ""))

    if instructions:
        blocks.append("<system_instructions>\n" + "\n\n".join(instructions) + "\n</system_instructions>")

    if simple:
        blocks.append("<user_message>\n" + convo[0].text + "\n</user_message>")
        blocks.append("Write the assistant's reply to the user message above.")
        return "\n\n".join(blocks)

    call_names: Dict[str, str] = {}
    lines: List[str] = ["<conversation>"]
    for t in convo:
        if t.role == "assistant":
            body = t.text
            if t.tool_calls:
                for c in t.tool_calls:
                    call_names[c.id] = c.name
                body = (body + "\n" if body else "") + _render_tool_calls(t.tool_calls)
            lines.append("<assistant>\n" + body + "\n</assistant>")
        elif t.role == "tool":
            name = t.name or call_names.get(t.tool_call_id or "", "")
            attrs = f' tool_call_id="{t.tool_call_id}"' if t.tool_call_id else ""
            attrs += f' name="{name}"' if name else ""
            lines.append(f"<tool_result{attrs}>\n" + t.text + "\n</tool_result>")
        else:
            lines.append("<user>\n" + t.text + "\n</user>")
    lines.append("</conversation>")
    blocks.append("\n".join(lines))

    if tools_active:
        blocks.append(render_tools_block(req))

    if rf.kind == "json_object":
        blocks.append("Output format: respond with a single valid JSON object only. "
                      "No prose before or after it, and no markdown code fences.")
    elif rf.kind == "json_schema":
        schema_text = _json(rf.schema or {})
        name = f" named \"{rf.name}\"" if rf.name else ""
        blocks.append(f"Output format: respond with a single valid JSON value that conforms to the JSON Schema{name} below. "
                      "Output only the JSON: no prose before or after it, and no markdown code fences.\n"
                      f"<json_schema>\n{schema_text}\n</json_schema>")

    blocks.append("Now write the assistant's next reply.")
    return "\n\n".join(blocks)


def render_tools_block(req: BridgeRequest) -> str:
    specs = []
    for t in req.tools:
        spec: Dict[str, Any] = {"name": t.name}
        if t.description:
            spec["description"] = t.description
        spec["parameters"] = t.parameters or {"type": "object", "properties": {}}
        specs.append(spec)
    rules = [
        "Function calling: the caller can run the functions listed in <available_functions>.",
        "- To call functions, reply with ONLY a JSON object of exactly this shape, with no prose and no code fences:",
        '  {"tool_calls": [{"name": "<function name>", "arguments": {<arguments that match the function\'s parameters schema>}}]}',
    ]
    if req.parallel_tool_calls:
        rules.append("- You may include several entries in tool_calls to call functions in parallel.")
    else:
        rules.append("- Call at most one function per reply.")
    rules.append("- If no function call is needed, reply normally in plain text.")
    rules.append("- Results of earlier calls appear in the conversation as <tool_result> blocks. Use them; do not repeat a call that already has a result unless needed.")
    choice = req.tool_choice
    if choice == "required":
        rules.append("- In this reply you MUST call at least one function.")
    elif isinstance(choice, dict) and choice.get("name"):
        rules.append(f"- In this reply you MUST call the function \"{choice['name']}\".")
    return ("<available_functions>\n" + json.dumps(specs, ensure_ascii=False, indent=2)
            + "\n</available_functions>\n\n" + "\n".join(rules))


# ---------------------------------------------------------------------------- 応答の後処理

_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_\-]*\s*\n(.*?)\n?\s*```\s*$", re.S)


def strip_code_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def clean_json_output(text: str) -> str:
    """JSON モードの応答から、コードフェンスや前後の説明文を取り除く。"""
    s = strip_code_fence(text.strip()).strip()
    try:
        json.loads(s)
        return s
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.S)
    if m:
        cand = m.group(1).strip()
        try:
            json.loads(cand)
            return cand
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                obj, end = decoder.raw_decode(s[i:])
                return s[i:i + end]
            except ValueError:
                continue
    return s


def _normalize_call(c: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(c, dict):
        return None
    fn = c.get("function") if isinstance(c.get("function"), dict) else {}
    name = c.get("name") or fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = c.get("arguments", fn.get("arguments", c.get("parameters", c.get("args", {}))))
    if isinstance(args, str):
        try:
            args_str = json.dumps(json.loads(args), ensure_ascii=False)
        except ValueError:
            args_str = args
    else:
        args_str = json.dumps(args if args is not None else {}, ensure_ascii=False)
    return name, args_str


def parse_tool_calls(text: str, tool_names: List[str]) -> Tuple[str, Optional[List[ToolCall]]]:
    """応答テキストから関数呼び出しを取り出す。戻り値は (呼び出し以外のテキスト, 呼び出し一覧 or None)。"""
    stripped = text.strip()
    if "{" not in stripped:
        return text, None
    candidates: List[Tuple[int, int, Any]] = []
    whole = strip_code_fence(stripped).strip()
    try:
        candidates.append((0, len(stripped), json.loads(whole)))
    except ValueError:
        decoder = json.JSONDecoder()
        idx = stripped.find("{")
        while idx != -1:
            try:
                obj, end = decoder.raw_decode(stripped[idx:])
                if isinstance(obj, dict) and "tool_calls" in obj:
                    candidates.append((idx, idx + end, obj))
                    break
            except ValueError:
                pass
            idx = stripped.find("{", idx + 1)

    for start, end, obj in candidates:
        raw_calls: Any
        if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
            raw_calls = obj["tool_calls"]
        elif isinstance(obj, dict) and start == 0 and end == len(stripped) and ("name" in obj and ("arguments" in obj or "parameters" in obj)):
            raw_calls = [obj]
        elif isinstance(obj, list) and start == 0 and all(isinstance(x, dict) and "name" in x for x in obj) and obj:
            raw_calls = obj
        else:
            continue
        calls: List[ToolCall] = []
        for rc in raw_calls:
            norm = _normalize_call(rc)
            if norm is None:
                continue
            if tool_names and norm[0] not in tool_names:
                continue
            calls.append(ToolCall(id=new_id("call_"), name=norm[0], arguments=norm[1]))
        if calls:
            rest = (stripped[:start] + stripped[end:]).strip()
            rest = re.sub(r"```[A-Za-z]*\s*```", "", rest).strip()
            return rest, calls
    return text, None


# ---------------------------------------------------------------------------- トークン数の概算

def estimate_tokens(text: str) -> int:
    """厳密なトークナイザは使えないため、ASCII は 4 文字 ≒ 1 トークン、それ以外は 1 文字 ≒ 1 トークンで概算する。"""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + other)
