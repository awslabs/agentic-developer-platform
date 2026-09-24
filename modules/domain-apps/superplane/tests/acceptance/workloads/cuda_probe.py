"""The same bounded CUDA computation runs on each demo provider through EKS."""

import json
from pathlib import Path


def probe():
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA device required; CPU fallback is not a pass")
    with torch.inference_mode():
        left = torch.ones((256, 256), dtype=torch.float32, device="cuda")
        right = torch.full((256, 256), 2, dtype=torch.float32, device="cuda")
        product = left @ right
        torch.cuda.synchronize()
        checksum = product.sum().item()
        if not torch.all(product == 512).item() or checksum != 33_554_432:
            raise RuntimeError("CUDA computation did not produce the expected result")
    return {
        "probe": "superplane-cuda-matmul-v1",
        "status": "succeeded",
        "device": str(torch.cuda.get_device_name(0))[:256],
        "device_kind": "cuda",
        "dtype": "float32",
        "shape": [256, 256],
        "element_value": 512,
        "checksum": int(checksum),
    }


def main():
    try:
        result = probe()
    except Exception as error:
        # Driver errors can contain host configuration. Keep the public failure
        # bounded; a failed probe never produces a successful workload exit.
        result = {
            "probe": "superplane-cuda-matmul-v1",
            "status": "failed",
            "error_type": type(error).__name__,
        }
    text = json.dumps(result, sort_keys=True, separators=(",", ":"))
    document = json.dumps({"superplane_result_version": 1, "text": text})
    if len(document.encode("utf-8")) >= 4096:
        raise RuntimeError("probe result exceeds the retained batch result limit")
    Path("/dev/termination-log").write_text(document, encoding="utf-8")
    print(text, flush=True)
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
