# 10 — Heterogeneous GPUs (CUDA + ROCm)

The first test farm for this project mixes an NVIDIA GeForce RTX 5070 Ti (16 GB, CUDA) and an AMD Radeon AI PRO R9700
(32 GB, ROCm). A farm router that assumes "all GPUs are the same" would send a job to a worker that cannot run it well —
or at all.

## Routing model

**Groups stay the primary key.** A group means "same custom nodes and models"; that is what decides whether a workflow
*can* run. Hardware is a secondary constraint:

- Each worker has `capabilities` detected from `/system_stats` (P15) and refreshed on reconnect:
  ```json
  {"backend": "cuda|rocm|cpu|mps", "torch": "2.x+cu128", "devices": [{"name": "...", "vram_total": 34359738368}],
   "comfyui": "0.41.0", "os": "posix|nt"}
  ```
  `backend` is derived from the PyTorch version string (`+rocm…` vs `+cu…`) because ROCm builds report device type
  `cuda` as well. *(unverified on our R9700 — check on the test box.)*
- A group may declare `requirements` (`{"backend": ["cuda"], "min_vram_gb": 24}`); a job may add its own (submitted by
  the frontend extension or an API client). The dispatcher only pairs a job with a worker whose capabilities satisfy
  both.
- A job whose requirements no online worker can satisfy is not "stuck silently": the health endpoint reports
  `unsatisfiable_pending` per group.

Typical setups:
- One group per backend (`video-cuda`, `video-rocm`) when the node set or settings differ (e.g. attention kernels
  that exist only for CUDA).
- One mixed group with per-job requirements when the same workflows run on both.

## Backend-specific behaviour the router must tolerate

| Topic | CUDA (5070 Ti, Blackwell) | ROCm (R9700, RDNA4 gfx1201) | Router impact |
|---|---|---|---|
| Model load blocking (P14) | 16 GB < 17 GB fp8 WAN model → weights streamed, longer loads | 32 GB fits the model → fewer loads | busy thresholds are per worker configurable |
| First-run kernel tuning | small | MIOpen searches kernels on new tensor shapes; repeated runs of video VAEs reported 4–5× slower until restart (ComfyUI #12672 / ROCm #6008, open as of 2026-10) | optional **recycle policy**: restart a worker after N jobs or when a job's duration exceeds the worker's recent median by a factor; implemented with the rollout drain machinery |
| FP8 | native on Blackwell | stored fp8 weights are upcast; some stacks silently fall back to FP32 (TransformerEngine #520) | none (informational on the dashboard) |
| Attention kernels | SageAttention etc. available | community ports only | express as group requirements |
| Driver/OS | Windows or Linux | Linux recommended (Windows: PyTorch only, no training) | Windows workers supported by the adapter; the upgrade contract is Linux-oriented (07) |

## Per-backend measurements to collect on the test box

- Load-block duration (P14) for the same workflow on both cards.
- Whether the ROCm repeated-run slowdown reproduces with our workflows (run the same job 3× back to back).
- Steady-state seconds per step for a reference workflow → used to show fair ETA per backend.
