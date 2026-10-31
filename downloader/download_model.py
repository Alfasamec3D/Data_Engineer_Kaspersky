#!/usr/bin/env python3
"""Initial download of model weights from Hugging Face into a mounted volume (PVC).

Idempotent: if the `.download_complete` marker already exists, the network is not
touched at all, so on pod restarts the model is served from the volume.

Environment:
  MODEL_REPO     - HF repo id, e.g. Qwen/Qwen2.5-1.5B-Instruct          (required)
  MODEL_DIR      - where to put the weights, e.g. /models/Qwen2.5-1.5B-Instruct (required)
  MODEL_REVISION - branch/tag/commit (default: main; a commit hash is best for reproducibility)
  HF_TOKEN       - HF token (needed for gated/private models), comes from the k8s Secret
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = ".download_complete"


def main() -> int:
    repo = os.environ.get("MODEL_REPO")
    target = os.environ.get("MODEL_DIR")
    if not repo or not target:
        print("MODEL_REPO and MODEL_DIR must be set", file=sys.stderr)
        return 2
    revision = os.environ.get("MODEL_REVISION", "main")
    target_dir = Path(target)
    marker = target_dir / MARKER

    if marker.exists():
        print(f"[download] {repo} already present in {target_dir}, skipping network access")
        return 0

    # imported here so the fast (cached) path does not depend on the library
    from huggingface_hub import snapshot_download

    print(f"[download] fetching {repo}@{revision} -> {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo,
        revision=revision,
        local_dir=str(target_dir),
        token=os.environ.get("HF_TOKEN") or None,
        # PyTorch .bin weights are not needed when safetensors exist; skip gguf/onnx etc.
        allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt", "*.tiktoken", "*.py"],
    )
    if not any(target_dir.glob("*.safetensors")):
        print("[download] ERROR: no *.safetensors files downloaded", file=sys.stderr)
        return 1
    # the marker is written last: a partial download never passes the check
    marker.write_text(f"{repo}@{revision}\n")
    print("[download] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
