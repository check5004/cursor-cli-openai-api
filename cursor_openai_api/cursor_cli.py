"""Cursor CLI（agent / cursor-agent）をサブプロセスとして実行し、stream-json を解釈する。"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

from .config import Config, is_windows
from .errors import BridgeError

log = logging.getLogger("cursor_openai_api")

KEEPALIVE_SECONDS = 15.0
WINDOWS_MAX_ARG_CHARS = 24_000
ATTACH_DIR = "attachments"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_MODEL_LINE_RE = re.compile(r"^\s*(?:[-*•>]\s+)?([A-Za-z0-9][A-Za-z0-9._:/+\-]*)\s+[-–—:]\s+(.+?)\s*$")
_MODEL_ONLY_RE = re.compile(r"^\s*(?:[-*•>]\s+)?([a-z0-9][a-z0-9._:/+\-]*[a-z0-9])\s*(?:\((?:current|default)[^)]*\))?\s*$")
_NOT_MODEL_WORDS = {"tip", "note", "available", "models", "usage", "error", "loading", "warning", "current", "default"}


@dataclass
class Attachment:
    filename: str
    data: bytes


@dataclass
class RunResult:
    text: str
    session_id: Optional[str]
    cursor_model: Optional[str]
    duration_ms: int


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "")


def parse_models_output(text: str) -> List[Dict[str, str]]:
    """`agent models` / `agent --list-models` の出力からモデル ID を取り出す。

    出力形式は CLI のバージョンで変わりうるため、JSON・`id - 表示名`・ID のみの行に対応する。
    """
    text = strip_ansi(text or "").strip()
    if not text:
        return []
    # JSON 形式で返ってきた場合
    if text[0] in "[{":
        try:
            data = json.loads(text)
            items = data.get("models", data.get("data", [])) if isinstance(data, dict) else data
            out = []
            for item in items:
                if isinstance(item, str):
                    out.append({"id": item, "name": item})
                elif isinstance(item, dict) and (item.get("id") or item.get("name")):
                    mid = str(item.get("id") or item.get("name"))
                    out.append({"id": mid, "name": str(item.get("displayName") or item.get("name") or mid)})
            return _dedupe(out)
        except ValueError:
            pass
    models: List[Dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _MODEL_LINE_RE.match(line)
        if m:
            mid, name = m.group(1), m.group(2)
            if mid.lower().rstrip(":") in _NOT_MODEL_WORDS:
                continue
            name = re.sub(r"\s*\((?:current|default)[^)]*\)\s*$", "", name).strip() or mid
            models.append({"id": mid, "name": name})
            continue
        m = _MODEL_ONLY_RE.match(line)
        if m and m.group(1).lower() not in _NOT_MODEL_WORDS and not line.endswith(":"):
            models.append({"id": m.group(1), "name": m.group(1)})
    return _dedupe(models)


def _dedupe(models: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for m in models:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        out.append(m)
    return out


def attachment_ref(cfg: Config, request_id: str, filename: str) -> str:
    """添付ファイルを、CLI の作業ディレクトリからの相対パスで表す。"""
    parts = [".cursor-openai-api", request_id] if cfg.workspace else []
    return "./" + "/".join(parts + [ATTACH_DIR, filename])


def _subprocess_env(cmd: Optional[List[str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("FORCE_COLOR", "0")
    env.setdefault("NO_OPEN_BROWSER", "1")
    if is_windows() and cmd and os.path.basename(cmd[0]).lower() == "node.exe":
        # 公式ランチャー（cursor-agent.cmd / .ps1）が設定する環境変数を再現する
        env.setdefault("CURSOR_INVOKED_AS", "cursor-agent.cmd")
        if env.get("LOCALAPPDATA"):
            env.setdefault("NODE_COMPILE_CACHE", os.path.join(env["LOCALAPPDATA"], "cursor-compile-cache"))
    return env


def _with_launcher(cmd: List[str]) -> List[str]:
    """Windows の .ps1 は PowerShell 経由で起動する。"""
    if is_windows() and cmd and cmd[0].lower().endswith(".ps1"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"] + cmd
    return cmd


def _popen_kwargs() -> Dict[str, Any]:
    if is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if is_windows():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def classify_cli_failure(stderr: str, stdout_tail: str = "", returncode: Optional[int] = None) -> BridgeError:
    """CLI の失敗内容から、OpenAI 形式のエラーに変換する。"""
    detail = strip_ansi((stderr or "").strip() or (stdout_tail or "").strip())
    if len(detail) > 2000:
        detail = detail[-2000:]
    low = detail.lower()
    if any(k in low for k in ("not authenticated", "not logged in", "please log in", "please login",
                               "authentication required", "unauthorized", "agent login", "invalid api key")):
        return BridgeError(
            401,
            "Cursor CLI が未ログインです。WSL で `agent login`（または `cursor-agent login`）を実行してから再試行してください。"
            f" CLI の出力: {detail}",
            "authentication_error",
            code="cursor_not_authenticated",
        )
    if "model" in low and any(k in low for k in ("not valid", "invalid", "not found", "not available",
                                                 "unknown model", "cannot use", "not supported")):
        return BridgeError(
            400,
            f"Cursor CLI がモデル指定を受け付けませんでした。`agent models` で使えるモデル ID を確認してください。 CLI の出力: {detail}",
            "invalid_request_error",
            code="model_not_found",
            param="model",
        )
    if any(k in low for k in ("workspace trust", "trust this", "--trust")):
        return BridgeError(
            500,
            "Cursor CLI がワークスペースの信頼確認で止まりました。--agent-flags に --trust を含めてください。"
            f" CLI の出力: {detail}",
            code="cursor_workspace_trust",
        )
    if any(k in low for k in ("unknown option", "unknown argument", "unrecognized", "error: option")):
        return BridgeError(
            500,
            "Cursor CLI が未対応のオプションを受け取りました。CLI を `agent update` で更新するか、"
            f"--agent-flags（CURSOR_AGENT_FLAGS）を調整してください。 CLI の出力: {detail}",
            code="cursor_unknown_option",
        )
    if any(k in low for k in ("rate limit", "usage limit", "quota", "too many requests")):
        return BridgeError(429, f"Cursor の利用上限に達しました。 CLI の出力: {detail}",
                           "rate_limit_error", code="rate_limit_exceeded")
    msg = f"Cursor CLI がエラー終了しました（終了コード {returncode}）。"
    if detail:
        msg += f" CLI の出力: {detail}"
    return BridgeError(500, msg, code="cursor_cli_error")


def _extract_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type", "text") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts)


class CursorCLI:
    """Cursor CLI の呼び出しをまとめたクラス。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._sem = threading.BoundedSemaphore(cfg.max_concurrency)
        self._models_lock = threading.Lock()
        self._models: Optional[List[Dict[str, str]]] = None
        self._models_at = 0.0
        self._models_error: Optional[str] = None

    # ------------------------------------------------------------------ 情報取得
    def command(self) -> List[str]:
        return _with_launcher(self.cfg.resolved_cursor_cmd())

    def run_simple(self, args: List[str], timeout: int = 60) -> Tuple[int, str, str]:
        cmd = self.command() + args
        workdir = tempfile.mkdtemp(prefix="cursor-openai-api-")
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=workdir, env=_subprocess_env(cmd), timeout=timeout,
            )
            return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def version(self) -> str:
        code, out, err = self.run_simple(["--version"], timeout=30)
        return strip_ansi(out or err).strip()

    def status(self) -> Tuple[int, str]:
        code, out, err = self.run_simple(["status"], timeout=60)
        return code, strip_ansi((out + "\n" + err).strip())

    def fetch_models(self) -> List[Dict[str, str]]:
        errors = []
        for args in (["models"], ["--list-models"]):
            try:
                code, out, err = self.run_simple(args, timeout=90)
            except subprocess.TimeoutExpired:
                errors.append(f"`{' '.join(args)}` がタイムアウトしました")
                continue
            models = parse_models_output(out)
            if models:
                return models
            detail = strip_ansi((err or out).strip())[-500:]
            errors.append(f"`{' '.join(args)}` (exit {code}): {detail}")
        raise RuntimeError("; ".join(errors) or "モデル一覧を取得できませんでした")

    def list_models(self, force: bool = False) -> List[Dict[str, str]]:
        """キャッシュ付きでモデル一覧を返す。取得に失敗した場合は空リスト。"""
        with self._models_lock:
            fresh = self._models is not None and (time.time() - self._models_at) < self.cfg.models_cache_ttl
            if fresh and not force:
                return list(self._models or [])
            try:
                self._models = self.fetch_models()
                self._models_error = None
            except (OSError, RuntimeError) as exc:
                self._models_error = str(exc)
                log.warning("Cursor のモデル一覧を取得できませんでした: %s", exc)
                if self._models is None:
                    self._models = []
            self._models_at = time.time()
            return list(self._models)

    @property
    def models_error(self) -> Optional[str]:
        return self._models_error

    def warm_models_async(self) -> None:
        threading.Thread(target=self.list_models, name="models-warmup", daemon=True).start()

    def known_model_ids(self) -> List[str]:
        # 起動時の先読みが進行中ならロックで待ち、未取得ならここで取得する
        return [m["id"] for m in self.list_models()]

    # ------------------------------------------------------------------ 実行
    def start_run(self, prompt: str, model: Optional[str], attachments: List[Attachment], request_id: str) -> "CursorRun":
        run = CursorRun(self, prompt, model, attachments, request_id)
        run.start()
        return run


class CursorRun:
    """1 回分の Cursor CLI 実行。events() で差分テキストを順に受け取る。"""

    def __init__(self, cli: CursorCLI, prompt: str, model: Optional[str],
                 attachments: List[Attachment], request_id: str) -> None:
        self.cli = cli
        self.cfg = cli.cfg
        self.prompt = prompt
        self.model = model
        self.attachments = attachments
        self.request_id = request_id
        self.proc: Optional[subprocess.Popen] = None
        self.session_id: Optional[str] = None
        self.cursor_model: Optional[str] = None
        self.file_mode = False
        self._tmpdir: Optional[str] = None
        self._workspace = ""
        self._rel_dir = os.path.join(".cursor-openai-api", request_id) if cli.cfg.workspace else ""
        self._queue: "queue.Queue[Tuple[str, bytes]]" = queue.Queue()
        self._stderr: Deque[str] = deque(maxlen=200)
        self._stdout_tail: Deque[str] = deque(maxlen=20)
        self._threads: List[threading.Thread] = []
        self._acquired = False
        self._started_at = 0.0
        self._closed = False

    # -------------------------------------------------------------- 準備
    def _prepare_workspace(self) -> None:
        if self.cfg.workspace:
            self._workspace = self.cfg.workspace
            os.makedirs(os.path.join(self._workspace, self._rel_dir), exist_ok=True)
        else:
            self._tmpdir = tempfile.mkdtemp(prefix="cursor-openai-api-")
            self._workspace = self._tmpdir
        if self.attachments:
            adir = os.path.join(self._workspace, self._rel_dir, ATTACH_DIR)
            os.makedirs(adir, exist_ok=True)
            for att in self.attachments:
                with open(os.path.join(adir, att.filename), "wb") as fh:
                    fh.write(att.data)

    def _decide_transport(self, cmd: List[str]) -> str:
        mode = self.cfg.prompt_transport
        if mode in ("arg", "file"):
            return mode
        if is_windows():
            # Windows のコマンドライン長の上限は 32767 文字（UTF-16）
            if len(self.prompt.encode("utf-16-le")) // 2 > min(self.cfg.max_arg_bytes, WINDOWS_MAX_ARG_CHARS):
                return "file"
            # .cmd/.bat/.ps1 経由では改行や引用符を含む引数を安全に渡せない
            if cmd and os.path.splitext(cmd[0])[1].lower() in (".cmd", ".bat", ".ps1"):
                return "file"
            return "arg"
        # Linux の 1 引数あたりの上限（128KiB）に余裕を持たせる
        if len(self.prompt.encode("utf-8")) > self.cfg.max_arg_bytes:
            return "file"
        return "arg"

    def _build_argv(self) -> List[str]:
        cmd = self.cli.command()
        argv = cmd + ["-p", "--output-format", "stream-json"]
        if self.cfg.stream_partial:
            argv.append("--stream-partial-output")
        argv += list(self.cfg.agent_flags)
        if self.model:
            argv += ["--model", self.model]
        if self._decide_transport(self.cfg.resolved_cursor_cmd()) == "file":
            self.file_mode = True
            rel = "/".join(p for p in (self._rel_dir.replace(os.sep, "/"), "prompt.md") if p)
            with open(os.path.join(self._workspace, rel), "w", encoding="utf-8") as fh:
                fh.write(self.prompt)
            argv.append(
                f"Read the file ./{rel} in the current directory. It contains the full request. "
                "Then respond exactly as it instructs. Do not mention the file or this instruction."
            )
        else:
            # "-" で始まるとオプションと解釈されるため、先頭に空白を入れて位置引数として渡す
            argv.append(" " + self.prompt if self.prompt.startswith("-") else self.prompt)
        return argv

    def start(self) -> None:
        if not self.cli._sem.acquire(timeout=max(1, self.cfg.timeout)):
            raise BridgeError(503, "同時実行数の上限に達しているため処理できませんでした。", code="server_busy", retryable=True)
        self._acquired = True
        try:
            self._prepare_workspace()
            argv = self._build_argv()
            if self.cfg.verbose:
                shown = argv[:-1] + ["<prompt %d chars>" % len(argv[-1])]
                log.info("[%s] exec: %s (cwd=%s)", self.request_id, " ".join(shown), self._workspace)
            if self.cfg.log_prompts:
                log.info("[%s] prompt:\n%s", self.request_id, self.prompt)
            self._started_at = time.time()
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self._workspace, env=_subprocess_env(argv), **_popen_kwargs(),
            )
        except FileNotFoundError as exc:
            self.close()
            raise BridgeError(500, str(exc) if "Cursor CLI" in str(exc) else f"Cursor CLI を起動できません: {exc}",
                              code="cursor_cli_not_found")
        except BaseException:
            self.close()
            raise
        self._threads = [
            threading.Thread(target=self._pump_stdout, name=f"stdout-{self.request_id}", daemon=True),
            threading.Thread(target=self._pump_stderr, name=f"stderr-{self.request_id}", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in iter(self.proc.stdout.readline, b""):
                self._queue.put(("line", line))
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(("eof", b""))

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            for line in iter(self.proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").rstrip()
                self._stderr.append(text)
                if self.cfg.verbose and text:
                    log.info("[%s] cli stderr: %s", self.request_id, strip_ansi(text))
        except (OSError, ValueError):
            pass

    # -------------------------------------------------------------- 実行結果
    def events(self) -> Iterator[Tuple[str, Any]]:
        """("delta", str) / ("keepalive", None) / ("done", RunResult) を順に返す。"""
        assert self.proc is not None
        deadline = self._started_at + self.cfg.timeout
        emitted = ""
        need_sep = False
        in_preamble = self.file_mode  # ファイル渡しのときは、プロンプトを読み終えるまでの発言を捨てる
        saw_tool = False
        result_text: Optional[str] = None
        result_error = False

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                kill_process_tree(self.proc)
                raise BridgeError(504, f"Cursor CLI の応答が {self.cfg.timeout} 秒以内に完了しませんでした。",
                                  code="timeout", retryable=False)
            try:
                kind, payload = self._queue.get(timeout=min(KEEPALIVE_SECONDS, remaining))
            except queue.Empty:
                yield ("keepalive", None)
                continue
            if kind == "eof":
                break
            line = payload.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                self._stdout_tail.append(line)
                if self.cfg.verbose:
                    log.info("[%s] cli stdout (non-json): %s", self.request_id, strip_ansi(line)[:300])
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "system":
                self.session_id = ev.get("session_id") or self.session_id
                self.cursor_model = ev.get("model") or self.cursor_model
            elif etype == "assistant":
                if self.cfg.stream_partial and ("timestamp_ms" not in ev or "model_call_id" in ev):
                    continue  # ツール呼び出し前・ターン終了時のまとめ送り（重複）は捨てる
                text = _extract_text(ev.get("message"))
                if not text or in_preamble:
                    continue
                if need_sep and emitted and not emitted.endswith("\n"):
                    text = "\n\n" + text
                need_sep = False
                emitted += text
                yield ("delta", text)
            elif etype == "tool_call":
                sub = ev.get("subtype")
                if sub == "started":
                    saw_tool = True
                    need_sep = True
                    if self.cfg.verbose:
                        names = list((ev.get("tool_call") or {}).keys())
                        log.info("[%s] cli tool call: %s", self.request_id, ",".join(names))
                elif sub == "completed" and in_preamble:
                    in_preamble = False
            elif etype == "result":
                result_error = bool(ev.get("is_error")) or ev.get("subtype") not in (None, "success")
                result_text = ev.get("result") if isinstance(ev.get("result"), str) else ""
                self.session_id = ev.get("session_id") or self.session_id
                break

        # プロセス終了を待つ（result の後は通常すぐ終了する）
        try:
            returncode = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill_process_tree(self.proc)
            returncode = self.proc.returncode
        for t in self._threads:
            t.join(timeout=2)

        if result_text is None or result_error:
            if result_error and result_text:
                raise classify_cli_failure(result_text, returncode=returncode)
            raise classify_cli_failure("\n".join(self._stderr), "\n".join(self._stdout_tail), returncode)

        if not emitted and result_text:
            # 差分が取れなかった場合（古い CLI など）は最終結果をまとめて返す
            emitted = result_text
            yield ("delta", result_text)
        elif not saw_tool and not self.file_mode and result_text.startswith(emitted) and len(result_text) > len(emitted):
            rest = result_text[len(emitted):]
            emitted += rest
            yield ("delta", rest)

        yield ("done", RunResult(
            text=emitted,
            session_id=self.session_id,
            cursor_model=self.cursor_model,
            duration_ms=int((time.time() - self._started_at) * 1000),
        ))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.proc is not None and self.proc.poll() is None:
            log.info("[%s] Cursor CLI を中断します", self.request_id)
            kill_process_tree(self.proc)
        if self.proc is not None:
            for stream in (self.proc.stdout, self.proc.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        elif self.cfg.workspace and self._rel_dir:
            shutil.rmtree(os.path.join(self._workspace, self._rel_dir), ignore_errors=True)
        if self._acquired:
            self._acquired = False
            self.cli._sem.release()
