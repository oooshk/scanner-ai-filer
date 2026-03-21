#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-http://127.0.0.1:8000}"
MODEL="${MODEL:-qwen2.5-instruct:1.5b}"
START_TIMEOUT="${START_TIMEOUT:-60}"
SKIP_PULL=false

usage() {
  cat <<'EOF'
Prepare a Hailo-10H Ollama-compatible LLM endpoint.

Usage:
  scripts/setup_hailo_ollama.sh [--host URL] [--model NAME] [--start-timeout SEC] [--skip-pull]

Defaults:
  --host http://127.0.0.1:8000
  --model qwen2.5-instruct:1.5b
  --start-timeout 60

Environment equivalents:
  HOST, MODEL, START_TIMEOUT
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --start-timeout)
      START_TIMEOUT="$2"
      shift 2
      ;;
    --skip-pull)
      SKIP_PULL=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required but not installed" >&2
  exit 1
fi

if ! command -v hailo-ollama >/dev/null 2>&1; then
  cat >&2 <<'EOF'
'hailo-ollama' was not found in PATH.

Install the Hailo GenAI Model Zoo package or copy a source-built hailo-ollama binary into PATH, then re-run this script.
EOF
  exit 1
fi

check_up() {
  curl --fail --silent --show-error --max-time 2 "$HOST/api/tags" >/dev/null
}

if check_up; then
  echo "[hailo-ollama] endpoint already running at $HOST"
else
  echo "[hailo-ollama] starting service in background"
  nohup hailo-ollama >/tmp/hailo-ollama.log 2>&1 &

  echo "[hailo-ollama] waiting for endpoint readiness"
  for _ in $(seq 1 "$START_TIMEOUT"); do
    if check_up; then
      break
    fi
    sleep 1
  done

  if ! check_up; then
    echo "[hailo-ollama] endpoint did not become ready within ${START_TIMEOUT}s" >&2
    echo "[hailo-ollama] see /tmp/hailo-ollama.log for details" >&2
    exit 1
  fi
fi

if [[ "$SKIP_PULL" != true ]]; then
  echo "[hailo-ollama] ensuring model is available: $MODEL"
  curl --fail --silent --show-error "$HOST/api/pull" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"stream\":false}" >/dev/null
fi

echo "[hailo-ollama] running chat smoke test"
RESP="$(curl --fail --silent --show-error "$HOST/api/chat" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in five words.\"}],\"stream\":false}" )"

echo "[hailo-ollama] endpoint ready at $HOST"
echo "[hailo-ollama] sample response:"
echo "$RESP"
