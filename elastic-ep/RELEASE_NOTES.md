# Release Notes

## v0.1.0

- **Version:** v0.1.0
- **Release Date:** 2026-07-16
- **Releaser:** a798347923

### Features

- Core fault tolerance framework with three-level sentinel hierarchy (ClientSentinel, EngineCoreSentinel, WorkerSentinel)
- ZMQ-based communication for fault reporting and instruction dispatch
- REST API for external fault tolerance control (`/fault_tolerance/apply`, `/fault_tolerance/status`)
- Graceful scale-down: pause affected ranks, redistribute experts, reload weights, reinitialize communication groups
- Dynamic EPLB integration for expert load balancing after fault
- External NPU hardware fault monitor via DCMI polling
- `--enforce-eager` mode support
- PIECEWISE ACL Graph mode support
- MTP (Multi-Token Prediction) support

### Tested Models

- DeepSeek-V3 (DSv3)
- Qwen3-235B-A22B
- GLM5

### Known Issues

During the **second scale-down**, the following issues may occasionally occur:

1. Fault weight loading time significantly increases
2. `stop device` cannot stop, blocking the scale-down flow
3. Worker stuck in `input_event` synchronization after recovery

### Patches

- `patches/vllm_scale_down.patch`: Core fault tolerance framework for vLLM v0.18.0
- `patches/vllm_ascend_scale_down.patch`: Ascend-specific adaptations for vllm-ascend v0.18.0
