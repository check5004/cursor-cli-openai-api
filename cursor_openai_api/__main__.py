"""コマンドライン入口: `python3 -m cursor_openai_api [serve|doctor|models] ...`"""

from __future__ import annotations

import logging
import os
import platform
import sys
import time
from typing import List, Optional

from . import __version__
from .config import Config, build_arg_parser, config_from_args, normalize_argv
from .cursor_cli import CursorCLI
from .engine import Engine
from .errors import BridgeError
from .prompt import BridgeRequest, Turn

log = logging.getLogger("cursor_openai_api")


def is_wsl() -> bool:
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def cmd_serve(cfg: Config) -> int:
    from .server import serve

    try:
        cmd = cfg.resolved_cursor_cmd()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    engine = Engine(cfg)
    try:
        httpd = serve(cfg, engine)
    except OSError as exc:
        log.error("ポート %s:%s で待ち受けできませんでした: %s", cfg.host, cfg.port, exc)
        return 1
    engine.cli.warm_models_async()

    host_shown = "localhost" if cfg.host in ("0.0.0.0", "127.0.0.1", "::") else cfg.host
    base = f"http://{host_shown}:{cfg.port}/v1"
    print(f"cursor-openai-api v{__version__}", flush=True)
    print(f"  Cursor CLI : {' '.join(cmd)}", flush=True)
    print(f"  Listening  : http://{cfg.host}:{cfg.port}  (base_url = {base})", flush=True)
    print(f"  Model      : {cfg.default_model or 'Cursor CLI の既定'}"
          + (f" / map: {cfg.model_map}" if cfg.model_map else ""), flush=True)
    print(f"  CLI flags  : {' '.join(cfg.agent_flags) or '(なし)'}", flush=True)
    print(f"  使い方     : OPENAI_BASE_URL={base} OPENAI_API_KEY={'<BRIDGE_API_KEY の値>' if cfg.api_key else 'dummy'}",
          flush=True)
    if is_wsl():
        print("  WSL        : Windows 側のアプリからも上記 URL（localhost）で接続できます。"
              "つながらない場合は --host 0.0.0.0 を試してください。", flush=True)
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not cfg.api_key:
        log.warning("127.0.0.1 以外で待ち受けていますが API キーが未設定です。"
                    "同じネットワークの誰でもあなたの Cursor 契約で実行できてしまうため、--api-key の設定を強く推奨します。")
    print("  停止       : Ctrl+C", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止します。", flush=True)
    finally:
        httpd.server_close()
    return 0


def cmd_models(cfg: Config) -> int:
    cli = CursorCLI(cfg)
    try:
        models = cli.fetch_models()
    except (OSError, RuntimeError) as exc:
        print(f"モデル一覧を取得できませんでした: {exc}", file=sys.stderr)
        return 1
    for m in models:
        print(f"{m['id']}\t{m['name']}" if m["name"] != m["id"] else m["id"])
    return 0


def cmd_doctor(cfg: Config, try_prompt: bool, model: Optional[str]) -> int:
    ok = True
    print(f"cursor-openai-api v{__version__}")
    print(f"[情報] Python {platform.python_version()} / {platform.system()} {platform.release()}"
          + (" (WSL)" if is_wsl() else ""))
    try:
        cmd = cfg.resolved_cursor_cmd()
    except FileNotFoundError as exc:
        print(f"[NG] {exc}")
        return 1
    print(f"[OK] Cursor CLI: {' '.join(cmd)}")
    cli = CursorCLI(cfg)
    try:
        print(f"[OK] バージョン: {cli.version() or '(不明)'}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[NG] バージョン取得に失敗: {exc}")
    try:
        code, out = cli.status()
        low = out.lower()
        logged_out = code != 0 or "not logged in" in low or "not authenticated" in low
        print(f"[{'NG' if logged_out else 'OK'}] ログイン状態:\n    " + "\n    ".join(out.splitlines()[:8]))
        if logged_out:
            ok = False
            print("    → `agent login`（または `cursor-agent login`）でログインしてください。"
                  " ブラウザが開かない場合は `NO_OPEN_BROWSER=1 agent login` で URL を表示できます。")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[NG] ログイン状態の確認に失敗: {exc}")
    try:
        models = cli.fetch_models()
        print(f"[OK] モデル {len(models)} 件: " + ", ".join(m["id"] for m in models[:15])
              + (" ..." if len(models) > 15 else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[注意] モデル一覧を取得できませんでした（--extra-models で手動指定できます）: {exc}")
    if try_prompt:
        engine = Engine(cfg, cli)
        req = BridgeRequest(turns=[Turn("user", "Reply with exactly the single word: OK")])
        t0 = time.time()
        try:
            final = engine.complete(req, model, "doctor")
            print(f"[OK] テスト実行 ({time.time() - t0:.1f} 秒, model={final.cursor_model or model or 'default'}): "
                  f"{final.content.strip()[:200]!r}")
        except BridgeError as exc:
            ok = False
            print(f"[NG] テスト実行に失敗 ({time.time() - t0:.1f} 秒): {exc.message}")
    print("問題は見つかりませんでした。" if ok else "上の [NG] を確認してください。")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_stdout()
    parser = build_arg_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:] if argv is None else argv))
    cfg = config_from_args(args)
    setup_logging(cfg.verbose)
    if args.command == "doctor":
        return cmd_doctor(cfg, args.try_prompt, args.model)
    if args.command == "models":
        return cmd_models(cfg)
    return cmd_serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
