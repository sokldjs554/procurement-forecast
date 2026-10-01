#!/bin/bash
set -euo pipefail
cd /tmp/procurement-local-llm
exec /tmp/procurement-local-llm/runtime/llama-b11308/llama-server \
  --model /tmp/procurement-local-llm/Qwen3-4B-Q4_K_M.gguf \
  --alias Qwen3-4B-Q4_K_M \
  --host 127.0.0.1 --port 18080 \
  --ctx-size 8192 --parallel 1 \
  --threads 6 --threads-batch 6 --gpu-layers 0 \
  --batch-size 512 --ubatch-size 256 \
  --jinja --chat-template-kwargs '{"enable_thinking":false}' \
  --reasoning off --no-webui \
  --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0 --presence-penalty 1.5 \
  > /tmp/procurement-local-llm/server.log 2>&1
