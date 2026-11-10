#!/usr/bin/env bash
# macOS / Linux / WSL 用の起動スクリプト。引数はそのままサーバーに渡されます。
#   ./scripts/start.sh --port 8765 --model-map "gpt-4o=auto"
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 が見つかりません。macOS は 'xcode-select --install' か Homebrew、Ubuntu/WSL は 'sudo apt install python3' で入れてください。" >&2
  exit 1
fi
exec "$PY" -m cursor_openai_api "$@"
