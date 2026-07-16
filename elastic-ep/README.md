# vLLM Scale-Down

[中文](README_ZH.md)

Elastic fault tolerance for [vLLM](https://github.com/vllm-project/vllm) on Huawei Ascend 910C NPU.

## Overview

vLLM Scale-Down enables vLLM to **survive NPU failures** without restarting. When a NPU card drops or becomes unhealthy, the system detects the fault, pauses affected data-parallel ranks, scales down by redistributing experts, and resumes serving on the remaining healthy NPUs.

## Key Features

| Feature | Description |
|---------|-------------|
| **Fault Reporting** | Engine crashes caught and reported via ZMQ within seconds |
| **Graceful Scale-Down** | Pauses affected ranks, redistributes experts, reloads weights |
| **External API Control** | REST API for pause, retry, and scale_down instructions |

## Latest Version

| Version | Date | Releaser |
|---------|------|----------|
| v0.1.0 | 2026-07-16 | a798347923 |

See [RELEASE_NOTES.md](RELEASE_NOTES.md) for full release history.

## Quick Start

### Prerequisites

- Huawei Ascend 910C
- Docker with access to `quay.io/ascend/vllm-ascend:v0.18.0-a3`
- DCMI library (optional, only for NPU hardware monitor)

### Installation

#### Step 1: Pull the Official Docker Image

```bash
docker pull quay.io/ascend/vllm-ascend:v0.18.0-a3
docker run -it --net=host --ipc=host --privileged=true \
    -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    quay.io/ascend/vllm-ascend:v0.18.0-a3 bash
```

#### Step 2: Apply the Patches

```bash
cd /vllm-workspace/vllm
git fetch --all && git checkout v0.18.0 && git reset --hard bcf2be96
git apply /path/to/patches/vllm_scale_down.patch

cd /vllm-workspace/vllm-ascend
git fetch --all && git checkout v0.18.0 && git reset --hard 4a533861
git apply /path/to/patches/vllm_ascend_scale_down.patch
```

#### Step 3: Install

```bash
cd /vllm-workspace/vllm
VLLM_TARGET_DEVICE=empty pip install -e .

cd /vllm-workspace/vllm-ascend
git submodule update --init --recursive
# Fix: triton-ascend 3.2.1 is only required for v0.20+; downgrade to 3.2.0
sed -i 's/triton-ascend==3.2.1/triton-ascend==3.2.0/' pyproject.toml
pip install -e .
```

### Usage

Reference scripts are provided under `examples/Fault-Tolerance-scale/`:

| Script | Description |
|--------|-------------|
| `serve_qwen.sh` | Start vLLM service with fault tolerance enabled |
| `scale_down.py` | NPU hardware fault monitor (optional) |

> **Note:** Before running `serve_qwen.sh`, you must configure the model parameters (`LOCAL_MODEL_PATH`, `MODEL_NAME`, etc.) in the script or override them via command-line arguments to match your environment.

#### Start vLLM Service

```bash
bash examples/Fault-Tolerance-scale/serve_qwen.sh \
    --dp 4 --re 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

#### Start the Monitor (Optional)

```bash
python examples/Fault-Tolerance-scale/scale_down.py \
    --npu-ids 0,1,2,3 --interval-time 3 \
    --external-fault-notify-port 22867 --port 8006
```

The monitor is optional. Without it, the framework still catches engine exceptions, auto-pauses, and waits for manual `retry` or `scale_down` via the REST API:

**Query current fault tolerance status:**

```bash
curl http://localhost:8006/fault_tolerance/status
```

**Retry (restart all DP ranks):**

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"retry"}'
```

**Scale down (exclude specific DP ranks):**

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"scale_down","params":{"timeout":30,"exclude_dp_ranks":[2]}}'
```

## Feature Status

| Feature | Status | Notes |
|---------|--------|-------|
| Dynamic EPLB | Fully supported | Expert placement re-balanced after fault via EPLB framework |
| Quantized models (W8A8) | Supported | Ascend-format W8A8 quantization adapted |
| MTP (Multi-Token Prediction) | Supported | Adapted and tested on GLM5 |
| PIECEWISE ACL Graph mode | Supported | Chunked graph capture for large models |

## Known Issues (v0.1.0)

During the **second scale-down**, the following issues may occasionally occur:

1. **Fault weight loading time significantly increases** — reloading weights after a second scale-down may take much longer than the first
2. **`stop device` cannot stop** — device pause may fail to complete, blocking the scale-down flow
3. **Worker stuck in `input_event` synchronization** — after recovery, workers may hang waiting on input event synchronization

## Tested Models

This feature has been verified on the following models:

- DeepSeek-V3 (DSv3)
- Qwen3-235B-A22B 
- GLM5

Other model types may have compatibility issues.

## Limitations

| Limitation | Description |
|------------|-------------|
| Ascend 910C only | Currently only supports Huawei Ascend 910C NPU |
| EP required | Must enable Expert Parallel (`--enable-expert-parallel`) to use fault tolerance features |
| TP = 1 only | Scale-down only supports tensor-parallel size of 1 |
| No scale-up | Cannot add NPU capacity back after scale-down |
| Pipeline parallel unsupported | PP is not compatible with scale-down |
| Redundant experts budget (scale-down) | Healthy cards' total redundant experts must exceed failed card's non-redundant experts |

## Documentation

| Document | Description |
|----------|-------------|
| [SPEC.md](SPEC.md) | Specifications, configuration, and REST API reference |
| [DESIGN.md](DESIGN.md) | Architecture and system design |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version release history |
| [TEST_REPORT.md](TEST_REPORT.md) | System test reports |

## License

Apache License 2.0
