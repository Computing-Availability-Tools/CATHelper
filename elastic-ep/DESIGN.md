# vLLM Scale-Down Design Document

## Architecture Overview

```
                          External Monitor
                          (scale_down.py)
                                |
                    +-----------+-----------+
                    |                       |
          ZMQ SUB (fault)        HTTP POST /fault_tolerance/apply
                    |                       |
                    v                       v
            +-------+-------+     +--------+--------+
            |  API Server   |     |  API Server     |
            |  (FastAPI)    |     |  (FastAPI)      |
            +-------+-------+     +--------+--------+
                    |                       |
                    v                       v
            +-------+-----------------------+--------+
            |          ClientSentinel                 |
            |  (one per vLLM instance)                |
            |  - Receives fault reports via ZMQ       |
            |  - Publishes engine health status       |
            |  - Dispatches pause/retry/scale_down    |
            +--+------------+------------+------------+
               |            |            |
      +--------+--+  +-----+------+  +--+--------+
      | EngineCore |  | EngineCore |  | EngineCore|
      | Sentinel   |  | Sentinel   |  | Sentinel  |
      | (DP rank 0)|  | (DP rank 1)|  | (DP rank 2)|
      +-----+------+  +-----+------+  +-----+-----+
            |                |               |
      +-----+----------------+---------------+-----+
      |           EngineCore (busy loop)            |
      |  wrapped with @fault_tolerant_wrapper       |
      +-----+----------------+---------------+-----+
            |                |               |
      +-----+------+  +-----+------+  +-----+------+
      |   Worker   |  |   Worker   |  |   Worker   |
      |  Sentinel  |  |  Sentinel  |  |  Sentinel  |
      |   (NPU)    |  |   (NPU)    |  |   (NPU)    |
      +------------+  +------------+  +------------+
```

## Sentinel Hierarchy

The fault tolerance framework uses a three-level sentinel architecture:

### 1. ClientSentinel (Top Level)

- One per vLLM instance, runs in the API server process
- Receives fault reports from all EngineCoreSentinels via ZMQ
- Publishes engine health status to external systems (ZMQ PUB)
- Dispatches fault tolerance instructions (pause/retry/scale_down) to engines
- Handles external REST API requests

### 2. EngineCoreSentinel (Middle Level)

- One per DP rank, runs in the EngineCore process
- Monitors engine exceptions via `fault_signal_q`
- Forwards fault info to ClientSentinel via ZMQ
- Receives and executes instructions from ClientSentinel
- Communicates with WorkerSentinels for worker-level operations

### 3. WorkerSentinel (Bottom Level)

- One per worker (NPU device), runs in the worker process
- Receives commands from EngineCoreSentinel via ZMQ
- Executes pause/retry operations at the NPU level
- Manages EP rank masks for fault-tolerant all2all communication

## Fault Tolerance Workflow

```
    NPU Card Drop / Engine Crash
                |
                v
    +-----------+-----------+
    | 1. DETECTION          |
    |   - DCMI polls health |
    |   - Engine reports    |
    |     via ZMQ           |
    +-----------+-----------+
                |
                v
    +-----------+-----------+
    | 2. PAUSE              |
    |   - Stop affected DP  |
    |     ranks from        |
    |     processing        |
    |   - Preempt in-flight |
    |     requests          |
    +-----------+-----------+
                |
                v
    +-----------+-----------+
    | 3. SCALE DOWN         |
    |   - Compute new rank  |
    |     mapping           |
    |   - Redistribute      |
    |     experts (EPLB)    |
    |   - Reload weights    |
    |     CPU -> NPU        |
    |   - Reinit comm       |
    |     groups (Gloo)     |
    +-----------+-----------+
                |
                v
    +-----------+-----------+
    | 4. RESUME             |
    |   - Remaining NPUs    |
    |     continue serving  |
    |   - Health status     |
    |     published         |
    +-----------+-----------+
```

## Two Operation Modes

### With External Monitor (NPU Hardware Faults)

The monitor (`scale_down.py`) uses DCMI to poll NPU health. When a card failure is detected:
1. Monitor sends `pause` instruction to freeze affected DP ranks
2. Monitor sends `scale_down` instruction to remove failed ranks
3. vLLM redistributes experts across remaining healthy NPUs

### Without Monitor (Any Engine Exception)

The fault tolerance framework catches exceptions in the engine busy loop:
1. `@fault_tolerant_wrapper` catches the exception
2. Engine is paused and waits for instructions (up to `engine_recovery_timeout_sec`)
3. User manually sends `retry` or `scale_down` via REST API

## Key Design Decisions

### ZMQ-Based Communication

All inter-component communication uses ZMQ sockets for:
- Low latency and high throughput
- decoupled producer-consumer patterns
- Support for async operations

### Stateful Health Tracking

ClientSentinel maintains `engine_status_dict` tracking each engine's state:
- `healthy` - Engine operating normally
- `paused` - Engine paused, waiting for instructions
- `unhealthy` - Engine encountered an error
- `dead` - Engine process has exited

### Graceful Degradation

When `engine_recovery_timeout_sec` expires without receiving instructions, the original exception is re-raised for standard error handling, ensuring the system fails predictably rather than hanging indefinitely.
