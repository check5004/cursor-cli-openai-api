"""テスト用ヘルパー: 偽 CLI を使ってサーバーを別スレッドで起動する。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cursor_openai_api.config import Config  # noqa: E402
from cursor_openai_api.server import BridgeServer  # noqa: E402

FAKE_CLI = os.path.join(ROOT, "tests", "fake_cursor_agent.py")


class RunningServer:
    def __init__(self, **overrides: Any) -> None:
        self.logfile = tempfile.NamedTemporaryFile(prefix="fake-cursor-", suffix=".jsonl", delete=False).name
        os.environ["FAKE_CURSOR_LOG"] = self.logfile
        cfg = Config(host="127.0.0.1", port=0, cursor_cmd=[sys.executable, FAKE_CLI])
        for k, v in overrides.items():
            setattr(cfg, k, v)
        self.cfg = cfg
        self.httpd = BridgeServer(cfg)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        try:
            os.unlink(self.logfile)
        except OSError:
            pass

    def calls(self) -> List[Dict[str, Any]]:
        with open(self.logfile, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def prompt_calls(self) -> List[List[str]]:
        return [c["argv"] for c in self.calls() if "-p" in c["argv"]]

    # ------------------------------------------------------------------ HTTP
    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def json(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        status, hdrs, raw = self.request(method, path, body, headers)
        return status, json.loads(raw.decode("utf-8")), hdrs

    def sse(self, path: str, body: Dict[str, Any]) -> List[Tuple[Optional[str], str]]:
        status, hdrs, raw = self.request("POST", path, body)
        assert status == 200, raw
        events: List[Tuple[Optional[str], str]] = []
        for block in raw.decode("utf-8").split("\n\n"):
            event = None
            data_lines = []
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
            if data_lines:
                events.append((event, "\n".join(data_lines)))
        return events
