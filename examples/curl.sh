#!/usr/bin/env bash
# curl での動作確認。サーバーを起動した状態で実行してください。
BASE="${OPENAI_BASE_URL:-http://localhost:8765/v1}"
KEY="${OPENAI_API_KEY:-dummy}"

echo "== /v1/models"
curl -s "$BASE/models" -H "Authorization: Bearer $KEY"; echo

echo "== /v1/chat/completions"
curl -s "$BASE/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"default","messages":[{"role":"user","content":"日本の首都は？一言で"}]}'; echo

echo "== streaming"
curl -sN "$BASE/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"default","stream":true,"messages":[{"role":"user","content":"1から5まで数えて"}]}'
