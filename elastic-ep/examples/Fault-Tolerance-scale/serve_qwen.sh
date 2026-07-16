#!/bin/bash

HOST="0.0.0.0"
PORT=8006
DATA_PARALLEL_SIZE=4
REDUNDANT_EXPERTS=0
FAULT_PORT=22867
LOCAL_MODEL_PATH="nytopop/Qwen3-30B-A3B.w8a8"
MODEL_NAME="/qwen-ai/Qwen3-30B-A3B-W8A8"
GLOO_TIMEOUT=30
RECOVERY_TIMEOUT=120

while [[ $# -gt 0 ]]; do
    case $1 in
        --dp)
            DATA_PARALLEL_SIZE="$2"
            shift 2
            ;;
        --re)
            REDUNDANT_EXPERTS="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --fault-port)
            FAULT_PORT="$2"
            shift 2
            ;;
        --model-name)
            MODEL_NAME="$2"
            shift 2
            ;;
        --local-model)
            LOCAL_MODEL_PATH="$2"
            shift 2
            ;;
        --gloo-timeout-seconds)
            GLOO_TIMEOUT="$2"
            shift 2
            ;;
        --recovery-timeout)
            RECOVERY_TIMEOUT="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --dp SIZE                      Set data parallel size (default: 4)"
            echo "  --re SIZE                      Set redundant experts (default: 0)"
            echo "  --host HOST                    Set host address (default: 0.0.0.0)"
            echo "  --port PORT                    Set port number (default: 8006)"
            echo "  --fault-port FAULT_PORT        Set external fault notify port (default: 22867)"
            echo "  --gloo-timeout-seconds GLOO_TIMEOUT    gloo communication group timeout (seconds)"
            echo "  --recovery-timeout RECOVERY_TIMEOUT    engine recovery timeout (seconds, default: 120)"
            echo "  --model-name MODEL_NAME        Set model name or path"
            echo "  --local-model LOCAL_MODEL_PATH Use local model at $LOCAL_MODEL_PATH"
            echo "  -h, --help                     Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use -h or --help for usage information"
            exit 1
            ;;
    esac
done

echo "Starting vLLM server for $MODEL_NAME with data parallel size: $DATA_PARALLEL_SIZE and redundant experts: $REDUNDANT_EXPERTS"

export DYNAMIC_EPLB="true"
export HCCL_BUFFSIZE=2048

vllm serve $LOCAL_MODEL_PATH \
    --served-model-name $MODEL_NAME \
    --data-parallel-size $DATA_PARALLEL_SIZE \
    --data-parallel-size-local $DATA_PARALLEL_SIZE \
    --enable-expert-parallel \
    --enable-fault-tolerance \
    --fault-tolerance-config '{"external_fault_notify_port":'$FAULT_PORT',"engine_recovery_timeout_sec":'$RECOVERY_TIMEOUT'}' \
    --api-server-count 1 \
    --trust-remote-code \
    --gloo-timeout-seconds $GLOO_TIMEOUT \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --additional-config '{"eplb_config":{"dynamic_eplb": true, "num_redundant_experts":'${REDUNDANT_EXPERTS}'}}' \
    --quantization ascend \
    --host $HOST \
    --port $PORT \
    --max-cudagraph-capture-size 512
