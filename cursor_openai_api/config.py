"""設定（コマンドライン引数と環境変数）。"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_PORT = 8765


def is_windows() -> bool:
    return sys.platform.startswith("win")
DEFAULT_AGENT_FLAGS = "--trust --mode ask"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"環境変数 {name} は整数で指定してください: {value!r}")


def parse_model_map(text: Optional[str]) -> Dict[str, str]:
    """`gpt-4o=sonnet-4.5,gpt-4o-mini=auto` 形式をパースする。"""
    result: Dict[str, str] = {}
    if not text:
        return result
    for item in text.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit(f"MODEL_MAP の書式が不正です（'名前=Cursorのモデル' で指定）: {item!r}")
        alias, target = item.split("=", 1)
        alias, target = alias.strip(), target.strip()
        if alias and target:
            result[alias] = target
    return result


def split_command(text: str) -> List[str]:
    """CLI の実行コマンドを分割する。存在するファイルパスならそのまま 1 要素として扱う。"""
    text = text.strip()
    if os.path.exists(text):
        cmd = [text]
    else:
        cmd = [t.strip('"') for t in shlex.split(text, posix=(os.name != "nt"))]
    if is_windows() and len(cmd) == 1:
        # Windows の .cmd / .ps1 ランチャーは、引数の改行や引用符を正しく渡せないため node.exe を直接使う
        direct = resolve_windows_node(os.path.dirname(os.path.abspath(cmd[0])))
        if direct and os.path.splitext(cmd[0])[1].lower() in (".cmd", ".bat", ".ps1"):
            return direct
    return cmd


_WIN_VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$")


def resolve_windows_node(base_dir: str) -> Optional[List[str]]:
    """Windows 版 Cursor CLI の node.exe と index.js を探す（公式ランチャー cursor-agent.ps1 と同じ手順）。

    インストール先は通常 %LOCALAPPDATA%\\cursor-agent で、versions\\<版>\\node.exe が本体。
    """
    if not base_dir or not os.path.isdir(base_dir):
        return None
    node = os.path.join(base_dir, "node.exe")
    index = os.path.join(base_dir, "index.js")
    if os.path.isfile(node) and os.path.isfile(index):
        return [node, index]
    versions = os.path.join(base_dir, "versions")
    if not os.path.isdir(versions):
        return None
    candidates = []
    for name in os.listdir(versions):
        m = _WIN_VERSION_RE.match(name)
        if not m:
            continue
        node = os.path.join(versions, name, "node.exe")
        index = os.path.join(versions, name, "index.js")
        if os.path.isfile(node) and os.path.isfile(index):
            date = int(m.group(1) + m.group(2).zfill(2) + m.group(3).zfill(2))
            candidates.append((date, m.group(4) or "", name, [node, index]))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def find_cursor_binary() -> Optional[List[str]]:
    """Cursor CLI の実行コマンドを探す。新しい名前は `agent`、旧名は `cursor-agent`。"""
    if is_windows():
        local = os.environ.get("LOCALAPPDATA", "")
        direct = resolve_windows_node(os.path.join(local, "cursor-agent")) if local else None
        if direct:
            return direct
    # 汎用的な名前の `agent` より、まず `cursor-agent` を優先する。
    for name in ("cursor-agent", "agent"):
        path = shutil.which(name)
        if path:
            if is_windows():
                direct = resolve_windows_node(os.path.dirname(path))
                if direct:
                    return direct
            return [path]
    # PATH に ~/.local/bin が入っていない環境（`bash -c` 起動や GUI からの起動など）向け。
    home = os.path.expanduser("~")
    for name in ("cursor-agent", "agent"):
        path = os.path.join(home, ".local", "bin", name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return [path]
    return None


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    api_key: Optional[str] = None
    cursor_cmd: Optional[List[str]] = None
    default_model: Optional[str] = None
    model_map: Dict[str, str] = field(default_factory=dict)
    strict_models: bool = False
    agent_flags: List[str] = field(default_factory=lambda: shlex.split(DEFAULT_AGENT_FLAGS))
    workspace: Optional[str] = None
    stream_partial: bool = True
    prompt_transport: str = "auto"  # auto | arg | file
    max_arg_bytes: int = 100_000
    timeout: int = 600
    max_concurrency: int = 4
    models_cache_ttl: int = 600
    extra_models: List[str] = field(default_factory=list)
    cors_origin: Optional[str] = None
    log_prompts: bool = False
    verbose: bool = False
    preamble: bool = True

    def resolved_cursor_cmd(self) -> List[str]:
        if self.cursor_cmd:
            return list(self.cursor_cmd)
        found = find_cursor_binary()
        if not found:
            raise FileNotFoundError(
                "Cursor CLI（agent / cursor-agent）が見つかりません。"
                " `curl https://cursor.com/install -fsS | bash` でインストールするか、"
                " --cursor-bin / CURSOR_AGENT_BIN でパスを指定してください。"
            )
        return found


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cursor-openai-api",
        description="Cursor CLI を OpenAI 互換 API（/v1/chat/completions など）として公開するローカルサーバー",
    )
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cursor-bin", default=_env("CURSOR_AGENT_BIN"),
                       help="Cursor CLI の実行ファイル（既定: cursor-agent → agent の順に自動検出） [env: CURSOR_AGENT_BIN]")
        p.add_argument("--agent-flags", default=_env("CURSOR_AGENT_FLAGS", DEFAULT_AGENT_FLAGS),
                       help=f"Cursor CLI に毎回渡す追加フラグ（既定: '{DEFAULT_AGENT_FLAGS}'） [env: CURSOR_AGENT_FLAGS]")
        p.add_argument("--models-cache-ttl", type=int, default=_env_int("MODELS_CACHE_TTL", 600),
                       help="モデル一覧のキャッシュ秒数 [env: MODELS_CACHE_TTL]")
        p.add_argument("--extra-models", default=_env("EXTRA_MODELS"),
                       help="モデル一覧に追加するモデルID（カンマ区切り）。一覧取得に失敗する環境向け [env: EXTRA_MODELS]")
        p.add_argument("-v", "--verbose", action="store_true", default=_env_bool("VERBOSE"),
                       help="詳細ログを出す [env: VERBOSE=1]")

    serve = sub.add_parser("serve", help="API サーバーを起動する（既定のサブコマンド）")
    add_common(serve)
    serve.add_argument("--host", default=_env("HOST", "127.0.0.1"),
                       help="待ち受けアドレス（既定: 127.0.0.1） [env: HOST]")
    serve.add_argument("--port", type=int, default=_env_int("PORT", DEFAULT_PORT),
                       help=f"待ち受けポート（既定: {DEFAULT_PORT}） [env: PORT]")
    serve.add_argument("--api-key", default=_env("BRIDGE_API_KEY"),
                       help="指定すると Authorization: Bearer <key> を必須にする [env: BRIDGE_API_KEY]")
    serve.add_argument("--default-model", default=_env("CURSOR_DEFAULT_MODEL"),
                       help="model 未指定・'default'・未知モデル時に使う Cursor のモデルID（既定: CLI の既定） [env: CURSOR_DEFAULT_MODEL]")
    serve.add_argument("--model-map", default=_env("MODEL_MAP"),
                       help="モデル名の読み替え。例: 'gpt-4o=sonnet-4.5,gpt-4o-mini=auto' [env: MODEL_MAP]")
    serve.add_argument("--strict-models", action="store_true", default=_env_bool("STRICT_MODELS"),
                       help="Cursor に無いモデル名を既定モデルに置き換えず 404 を返す [env: STRICT_MODELS=1]")
    serve.add_argument("--workspace", default=_env("CURSOR_WORKSPACE"),
                       help="Cursor CLI を実行するディレクトリ（既定: リクエストごとの空の一時ディレクトリ） [env: CURSOR_WORKSPACE]")
    serve.add_argument("--no-partial", action="store_true", default=_env_bool("NO_PARTIAL_OUTPUT"),
                       help="--stream-partial-output を使わない（ストリーミングがメッセージ単位になる） [env: NO_PARTIAL_OUTPUT=1]")
    serve.add_argument("--prompt-transport", choices=("auto", "arg", "file"), default=_env("PROMPT_TRANSPORT", "auto"),
                       help="プロンプトの渡し方。auto=通常は引数・巨大時はファイル [env: PROMPT_TRANSPORT]")
    serve.add_argument("--timeout", type=int, default=_env_int("REQUEST_TIMEOUT", 600),
                       help="1 リクエストあたりのタイムアウト秒数（既定: 600） [env: REQUEST_TIMEOUT]")
    serve.add_argument("--max-concurrency", type=int, default=_env_int("MAX_CONCURRENCY", 4),
                       help="Cursor CLI の同時実行数（既定: 4） [env: MAX_CONCURRENCY]")
    serve.add_argument("--cors-origin", default=_env("CORS_ORIGIN"),
                       help="ブラウザから直接呼ぶ場合に許可する Origin（例: http://localhost:5173 や *） [env: CORS_ORIGIN]")
    serve.add_argument("--no-preamble", action="store_true", default=_env_bool("NO_PREAMBLE"),
                       help="「API として振る舞って」という前置き指示をプロンプトに付けない [env: NO_PREAMBLE=1]")
    serve.add_argument("--log-prompts", action="store_true", default=_env_bool("LOG_PROMPTS"),
                       help="Cursor CLI に渡すプロンプト全文をログに出す（デバッグ用） [env: LOG_PROMPTS=1]")

    doctor = sub.add_parser("doctor", help="Cursor CLI のインストール・ログイン状態・モデル一覧を確認する")
    add_common(doctor)
    doctor.add_argument("--try-prompt", action="store_true",
                        help="実際に短いプロンプトを 1 回実行して疎通を確認する（利用枠を少し消費します）")
    doctor.add_argument("--model", default=None, help="--try-prompt で使うモデル")

    models = sub.add_parser("models", help="Cursor CLI で使えるモデル ID を表示する")
    add_common(models)
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    cfg = Config()
    if getattr(args, "cursor_bin", None):
        cfg.cursor_cmd = split_command(args.cursor_bin)
    cfg.agent_flags = shlex.split(args.agent_flags or "", posix=(os.name != "nt"))
    cfg.models_cache_ttl = args.models_cache_ttl
    cfg.extra_models = [m.strip() for m in (args.extra_models or "").split(",") if m.strip()]
    cfg.verbose = bool(args.verbose)
    if getattr(args, "command", None) in (None, "serve"):
        cfg.host = args.host
        cfg.port = args.port
        cfg.api_key = args.api_key
        cfg.default_model = args.default_model or None
        cfg.model_map = parse_model_map(args.model_map)
        cfg.strict_models = bool(args.strict_models)
        cfg.workspace = os.path.abspath(os.path.expanduser(args.workspace)) if args.workspace else None
        cfg.stream_partial = not args.no_partial
        cfg.prompt_transport = args.prompt_transport
        cfg.timeout = args.timeout
        cfg.max_concurrency = max(1, args.max_concurrency)
        cfg.cors_origin = args.cors_origin
        cfg.preamble = not args.no_preamble
        cfg.log_prompts = bool(args.log_prompts)
    return cfg


def normalize_argv(argv: List[str]) -> List[str]:
    """サブコマンド省略時は serve とみなす。"""
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        return ["serve"] + list(argv)
    return list(argv)
