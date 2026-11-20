"""Build-time model download for the spike image: only what the dense sentence-transformers path loads."""

import sys

from huggingface_hub import HfApi, snapshot_download

SKIP = ["onnx/*", "*.onnx", "openvino/*", "*.h5", "*.msgpack", "*.ot", "colbert_linear.pt", "sparse_linear.pt", "imgs/*"]

for model in sys.argv[1:]:
    files = HfApi().list_repo_files(model)
    # Prefer safetensors; keep pytorch_model.bin only for repos that ship no safetensors (e.g. BAAI/bge-m3).
    skip = SKIP + (["*.bin"] if any(f.endswith(".safetensors") for f in files) else [])
    snapshot_download(model, local_dir=f"/models/{model}", ignore_patterns=skip)
