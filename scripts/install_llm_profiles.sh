#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-/home/pi/models}"
ONLY="all"

usage() {
  cat <<'EOF'
Install recommended local LLM profile models for scanner-filer.

Usage:
  scripts/install_llm_profiles.sh [--model-dir PATH] [--only fast|balanced|deep|ultra|all]

Defaults:
  --model-dir /home/pi/models
  --only all

Profiles:
  fast     -> Qwen2.5-1.5B-Instruct-Q4_K_M.gguf
  balanced -> Qwen2.5-3B-Instruct-Q4_K_M.gguf
  deep     -> Qwen2.5-7B-Instruct-Q4_K_M.gguf
  ultra    -> Qwen2.5-14B-Instruct-Q4_K_M.gguf (16B-class)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model-dir)
      MODEL_DIR="$2"
      shift 2
      ;;
    --only)
      ONLY="$2"
      shift 2
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

if ! command -v wget >/dev/null 2>&1; then
  echo "wget is required but not found" >&2
  exit 1
fi

mkdir -p "$MODEL_DIR"

install_model() {
  local name="$1"
  local url="$2"
  local target="$MODEL_DIR/$name"
  echo "[models] downloading $name"
  wget -c -O "$target" "$url"
  echo "[models] ready: $target"
}

case "$ONLY" in
  fast|balanced|deep|ultra|all) ;;
  *)
    echo "Invalid --only value: $ONLY (expected fast|balanced|deep|ultra|all)" >&2
    exit 2
    ;;
esac

if [[ "$ONLY" == "fast" || "$ONLY" == "all" ]]; then
  install_model \
    "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf" \
    "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf?download=true"
fi

if [[ "$ONLY" == "balanced" || "$ONLY" == "all" ]]; then
  install_model \
    "Qwen2.5-3B-Instruct-Q4_K_M.gguf" \
    "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf?download=true"
fi

if [[ "$ONLY" == "deep" || "$ONLY" == "all" ]]; then
  install_model \
    "Qwen2.5-7B-Instruct-Q4_K_M.gguf" \
    "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf?download=true"
fi

if [[ "$ONLY" == "ultra" || "$ONLY" == "all" ]]; then
  install_model \
    "Qwen2.5-14B-Instruct-Q4_K_M.gguf" \
    "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf?download=true"
fi

echo "[models] completed profile install in $MODEL_DIR"
