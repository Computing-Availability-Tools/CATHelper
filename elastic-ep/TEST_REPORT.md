# Test Report

## v0.1.0

- **Version:** v0.1.0
- **Test Date:** 2026-07-16
- **Tester:** a798347923

### Test Environment

| Component | Version/Spec |
|-----------|--------------|
| Hardware | Huawei Ascend 910C |
| Docker Image | `quay.io/ascend/vllm-ascend:v0.18.0-a3` |
| vLLM | v0.18.0 |
| vllm-ascend | v0.18.0 |

### Tested Models

| Model | Quantization | Status |
|-------|--------------|--------|
| DeepSeek-V3 (DSv3) | FP16 | Verified |
| Qwen3-235B-A22B | W8A8 | Verified |
| he | FP16 | Verified |

### Test Results

| Test Case | Status | Notes |
|-----------|--------|-------|
| Service startup with fault tolerance | | |
| Engine health status reporting | | |
| Pause instruction execution | | |
| Retry instruction execution | | |
| Scale-down instruction execution | | |
| NPU hardware fault detection (with monitor) | | |
| Engine exception handling (without monitor) | | |
| REST API /fault_tolerance/status | | |
| REST API /fault_tolerance/apply | | |
| Multi-DP rank failover | | |
| Expert redistribution after scale-down | | |
| `--enforce-eager` mode | | |
| PIECEWISE ACL Graph mode | | |

### Summary

<!-- Fill in after testing -->
