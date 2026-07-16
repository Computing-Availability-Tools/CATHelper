# vLLM Scale-Down Specification

## System Requirements

| Component | Requirement |
|-----------|-------------|
| Hardware | Huawei Ascend 910C NPU |
| OS | Linux (Ascend-compatible) |
| Docker Image | `quay.io/ascend/vllm-ascend:v0.18.0-a3` |
| vLLM | v0.18.0 (with Ascend backend `vllm_ascend`) |
| DCMI Library | `/usr/local/dcmi/libdcmi.so` (optional, for NPU monitor) |
| Python Packages | `zmq`, `msgspec`, `requests` |

## Patches

| Patch | Target | Description |
|-------|--------|-------------|
| `patches/vllm_scale_down.patch` | `vllm-project/vllm` (v0.18.0) | Core fault tolerance framework: sentinel hierarchy, ZMQ communication, HTTP API, engine health monitoring, scale-down orchestration |
| `patches/vllm_ascend_scale_down.patch` | `vllm-project/vllm-ascend` (v0.18.0) | Ascend-specific: NPU worker sentinel, ScaleDownHelper (7-phase workflow), DCMI hardware monitoring, EPLB fault rearrangement policy |

## CLI Arguments

### vLLM Serve Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--enable-fault-tolerance` | `False` | Enable the fault tolerance framework |
| `--enable-expert-parallel` | `False` | Enable Expert Parallel (required for fault tolerance) |
| `--fault-tolerance-config` | `None` | JSON dict for FT config (auto-enables FT) |
| `--gloo-timeout-seconds` | `None` (falls back to 600) | Gloo process group timeout in seconds |

### FaultToleranceConfig

| Field | Default | Description |
|-------|---------|-------------|
| `engine_recovery_timeout_sec` | `120` | Seconds to wait for recovery instructions before re-raising the original error |
| `enable_fault_tolerance_rebalance` | `False` | Re-invoke EPLB for expert load balancing after fault |
| `internal_fault_report_port` | `22866` | Port for engines to report faults to ClientSentinel (internal) |
| `external_fault_notify_port` | `22867` | Port for ClientSentinel to publish fault notifications (external ZMQ PUB) |

### serve_qwen.sh Script Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--dp` | `4` | Data parallel size |
| `--re` | `0` | Redundant experts count |
| `--host` | `0.0.0.0` | Server host address |
| `--port` | `8006` | Server port |
| `--fault-port` | `22867` | External fault notify port |
| `--model-name` | `/qwen-ai/Qwen3-30B-A3B-W8A8` | Model name or path |
| `--local-model` | `nytopop/Qwen3-30B-A3B.w8a8` | Local model path |
| `--recovery-timeout` | `120` | Engine recovery timeout (seconds) |
| `--gloo-timeout-seconds` | `30` | Gloo communication group timeout (seconds) |

## REST API

### POST /fault_tolerance/apply

Send a fault tolerance instruction to the running vLLM instance.

**Request Body:**

```json
{
    "instruction": "pause | retry | scale_down",
    "params": {
        "timeout": 30,
        "exclude_dp_ranks": [2]
    }
}
```

**Instructions:**

| Instruction | Required Params | Optional Params | Description |
|-------------|-----------------|-----------------|-------------|
| `pause` | `timeout` | `exclude_engine_index` | Pause request processing on specified or all DP ranks |
| `retry` | `timeout` | — | Clean worker states and reinitialize communication |
| `scale_down` | `timeout`, `exclude_dp_ranks` | — | Remove specified DP ranks and redistribute experts |

### GET /fault_tolerance/status

Returns current engine health status.

**Response:**

```json
{
    "total_engines": 4,
    "engines": [
        {"id": 0, "status": "healthy"},
        {"id": 1, "status": "healthy"},
        {"id": 2, "status": "dead"},
        {"id": 3, "status": "healthy"}
    ]
}
```

## Feature Status

| Feature | Status | Notes |
|---------|--------|-------|
| Dynamic EPLB | Fully supported | Expert placement re-balanced after fault via EPLB framework |
| Quantized models (W8A8) | Supported | Ascend-format W8A8 quantization adapted |
| Quantized models (W4A8) | Not yet supported | W4A8 quantization not yet adapted |
| MTP (Multi-Token Prediction) | Supported | Adapted and tested on GLM5 |
| `--enforce-eager` mode | Supported | Disables graph capture, runs in eager mode |
| PIECEWISE ACL Graph mode | Supported | Chunked graph capture for large models |

## Communication Channels

| Channel | Protocol | Direction | Purpose |
|---------|----------|-----------|---------|
| Engine Fault Socket | ZMQ DEALER/ROUTER | Engine -> ClientSentinel | Report engine exceptions |
| Fault State PUB/SUB | ZMQ PUB/SUB | ClientSentinel -> External | Broadcast engine health status |
| FT Request/Result | ZMQ DEALER/PUSH | ClientSentinel -> Engine | Dispatch pause/retry/scale_down |
| Worker Command | ZMQ ROUTER/DEALER | EngineCore -> Worker | Worker-level control |
| HTTP API | REST | External -> API Server | External fault tolerance control |
