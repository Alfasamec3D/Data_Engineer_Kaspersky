#!/usr/bin/env bash
# Starts vLLM. All parameters come from the ConfigMap (env), secrets from the Secret (env).
# Settings can be changed without rebuilding the image.
set -euo pipefail

MODEL_DIR="${MODEL_DIR:?MODEL_DIR is not set}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-$(basename "$MODEL_DIR")}"

# the weights must be prepared by the initContainer (the marker is written when the download finishes)
if [[ ! -f "${MODEL_DIR}/.download_complete" ]]; then
  echo "ERROR: model not found in ${MODEL_DIR} (no .download_complete marker)" >&2
  exit 1
fi

# Client bearer token: vLLM reads it from VLLM_API_KEY itself (it never appears in process arguments)
if [[ -z "${VLLM_API_KEY:-}" ]]; then
  echo "ERROR: VLLM_API_KEY is empty; refusing to start an unauthenticated endpoint" >&2
  exit 1
fi

# tcmalloc + Intel OpenMP: recommended by vLLM for the CPU backend
tcmalloc="$(ldconfig -p | awk '/libtcmalloc_minimal\.so\.4/ {print $NF; exit}')"
iomp="$(find /opt/venv -name 'libiomp5.so' -print -quit 2>/dev/null || true)"
preload=""
[[ -n "${tcmalloc}" ]] && preload="${tcmalloc}"
[[ -n "${iomp}" ]] && preload="${preload:+${preload}:}${iomp}"
[[ -n "${preload}" ]] && export LD_PRELOAD="${preload}${LD_PRELOAD:+:${LD_PRELOAD}}"

args=(
  serve "${MODEL_DIR}"
  --served-model-name "${SERVED_MODEL_NAME}"
  --host 0.0.0.0
  --port "${PORT:-8000}"
  --dtype "${DTYPE:-bfloat16}"
  --max-model-len "${MAX_MODEL_LEN:-4096}"
  --max-num-seqs "${MAX_NUM_SEQS:-16}"
  --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS:-2048}"
  # log the fact of a request but not its content (extra safety on top of the log filter)
  --enable-log-requests
  --max-log-len 0
)

# arbitrary extra vLLM flags from the ConfigMap, e.g.: "--enable-prefix-caching"
if [[ -n "${EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  args+=(${EXTRA_ARGS})
fi

echo "Starting: vllm ${args[*]}"
exec vllm "${args[@]}"
