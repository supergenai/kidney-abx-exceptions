#!/usr/bin/env bash
# Start an OpenAI-compatible mlx_lm.server for a Hugging Face MLX model.
# Usage: scripts/serve_mlx.sh [HF_MODEL_ID] [PORT] [extra mlx_lm.server args...]
#   e.g. scripts/serve_mlx.sh mlx-community/Qwen3.5-4B-4bit 8080 --chat-template-args '{"enable_thinking":false}'
#   default model: mlx-community/Qwen2.5-3B-Instruct-4bit, default port: 8080
# Endpoint: http://localhost:$PORT/v1  (model weights download to ~/.cache/huggingface on first run)
set -euo pipefail
MODEL="${1:-mlx-community/Qwen2.5-3B-Instruct-4bit}"
PORT="${2:-8080}"
shift $(( $# < 2 ? $# : 2 ))
cd "$(dirname "$0")/.."
echo "serving $MODEL on http://127.0.0.1:$PORT/v1" >&2
exec uv run python -m mlx_lm server --model "$MODEL" --host 127.0.0.1 --port "$PORT" "$@"
