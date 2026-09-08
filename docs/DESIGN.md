# CATHelper 设计文档 (DESIGN)

> 本文档描述 CATHelper 软件的整体架构、分层模块设计、各上层特性设计以及特性之间的整合设计。
> 各子项目更详细的设计见各自的 DESIGN.md：[CATMonitor/DESIGN.md](../CATMonitor/DESIGN.md)、[feature/elastic-ep/DESIGN.md](../feature/elastic-ep/DESIGN.md)、[feature/straggler/DESIGN.md](../feature/straggler/DESIGN.md) 与 `DESIGN_NPU_RESOURCE.md`、[feature/accuracy-monitoring/design.md](../feature/accuracy-monitoring/design.md)。
> 功能规格见 [SPEC.md](../SPEC.md)，使用手册见 [User_Manual.md](../User_Manual.md)，版本记录见 [Release_Notes.md](../Release_Notes.md)。

---

## 1. 概述

### 1.1 项目定位

CATHelper 是 CAT（Computing Availability Tools）技术架构的主体部分，服务于鲲鹏和昇腾服务器，提供全栈故障指标采集、分析和容错恢复能力，方便被集成，以及使能大型生产环境的高可用特性开发。

采用 **"底座 + 上层特性"** 的分层架构：
- **底座（CATMonitor）**：提供全栈指标采集、健康度评估与 Prometheus 导出能力，并对外提供故障信息订阅/推送机制与 KPI 输出能力。
- **上层特性**：基于底座或独立面向推理高可用场景构建的特性，当前包含 **EEP（推理大 EP 卡级弹性容错）**、**Straggler（慢节点/慢卡检测）** 与 **Accuracy-Monitoring（推理精度异常检测）**。

### 1.2 版本信息

| 项目 | 说明 |
|------|------|
| CATHelper 版本 | v0.2.4（2026-09-08） |
| 底座 CATMonitor 版本 | v0.3.3（独立 Go module，`github.com/Computing-Availability-Tools/CATMonitor`） |
| EEP 版本 | v0.1.0（vLLM 容错框架补丁 + 外部故障管理中心） |
| Straggler 版本 | v0.2.3（独立 Go module，`github.com/Computing-Availability-Tools/CATHelper/feature/straggler`） |
| Accuracy-Monitoring 版本 | v0.2.0（vLLM `--middleware` ASGI 中间件，Python 包 `anomaly_middleware`） |
| 平台支持 | Linux (x86_64) 为主，CATMonitor 兼容 Windows；EEP/straggler daemon/accuracy-monitoring 需华为昇腾 A3 服务器 |
| 许可证 | Apache-2.0 |

### 1.3 顶层组成

```
CATHelper/
├── CATMonitor/            # 底座：全栈指标采集、健康度评估、Prometheus 导出守护进程
│   ├── internal/          #   采集核心 + 7 部件采集器 + 14 来源层 + 指标采集目录
│   ├── features/          #   health / snapshot / web / dfee / exporter / faultsub / stragglerout
│   └── configs/           #   catmonitor.yaml + metrics.yaml
├── feature/
│   ├── elastic-ep/        # 上层特性：推理大 EP 卡级弹性容错（EEP）
│   │   ├── patches/       #   vLLM + vLLM-Ascend 容错框架补丁
│   │   └── examples/      #   容错服务启动脚本 + 外部故障管理中心（订阅 CATMonitor）
│   ├── straggler/         # 上层特性：慢节点（慢卡）检测
│   │   ├── main.go        #   统一入口（一次性 + --daemon 守护进程）
│   │   ├── daemon/        #   守护进程：dyno/dynolog 采集 + 周期检测 + HTTP 查询/控制
│   │   ├── resource/      #   第一道 KPI 资源检测（空间 peer 对比 + 共享 kmeans）
│   │   ├── profiling/     #   第二道 Profiler 检测（读 Ascend .db）
│   │   ├── clustering/    #   共享 kmeans 比例检测算法
│   │   ├── build.sh       #   aarch64 一键构建（dyno/dynolog + Python wheel + go build）
│   │   └── 3rdparty/msmonitor  # msmonitor 子模块（build.sh 引用）
│   └── accuracy-monitoring/  # 上层特性：推理精度异常检测（vLLM 中间件，Python）
│       ├── anomaly_middleware/  # ASGI 中间件包（拦截/抽取/恢复/检测调度）
│       ├── webui/              # 多实例聚合可视化 + 阈值告警（钉钉/飞书/企业微信/邮箱）
│       ├── configs/            # detector.yaml（算法阈值）+ webui.yaml
│       └── tests/             # 单元测试 + e2e 测试
├── docs/                  # 本设计文档、CI 门禁等
├── SPEC.md                # 功能规格说明书
├── User_Manual.md         # 使用手册
├── Release_Notes.md       # 版本发布记录
└── README.md              # 项目说明
```

> **模块独立性**：CATMonitor 子目录为主干快照，保持独立 Go module，可在 `CATMonitor/` 内独立 `go build`/`make build`；straggler 同样是独立 Go module（`feature/straggler/`，依赖 `modernc.org/sqlite`，无 CGo）；EEP 是 Python + vLLM 补丁形态；accuracy-monitoring 是 Python ASGI 中间件包（`pip install -e .`）。四者均"不互相 import"，仅通过文件/REST/Webhook/vLLM 插件契约耦合，便于独立演进与跨机部署。

---

## 2. 整体架构

### 2.1 分层架构总览

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                      上层特性（容错恢复 / 性能劣化检测 / 精度异常检测）        │
│   ┌─────────────────────────┐ ┌──────────────────────────┐ ┌──────────────┐ │
│   │ EEP 推理卡级弹性容错     │ │ Straggler 慢节点/慢卡检测 │ │ Accuracy-    │ │
│   │ - vLLM 三级哨兵容错框架   │ │ - 第一道：KPI 资源检测    │ │ Monitoring   │ │
│   │ - 外部故障管理中心 demo  │ │   （空间 peer 对比）      │ │ vLLM ASGI    │ │
│   │ - catmonitor_fault_sub  │ │ - 第二道：Profiler 检测    │ │ --middleware │ │
│   │   订阅 CATMonitor 故障   │ │ - 守护进程 --daemon（HTTP）│ │ 透明拦截检测  │ │
│   └──────────────┬──────────┘ └──────────────┬───────────┘ └──────┬───────┘ │
│                  │ ① HTTP Webhook（FaultEvent） │ ② KPI JSONL 文件   │ ③ vLLM │
│                  │   CATMonitor → EEP 订阅器    │   CATMonitor →     │ 请求/  │
│                  │   + REST 注册/快照/事件回补   │   straggler        │ 响应  │
├──────────────────┼─────────────────────────────┼────────────────────┼────────┤
│                  ▼                             ▼                    ▼        │
│   ┌────────────────────────────────────────────────────────────────────────┐ │
│   │              CATMonitor 底座（守护进程 daemon，Go）                     │ │
│   │  ┌──────────────────────────────────────────────────────────────────┐  │ │
│   │  │ features/ 特性层（基于采集基础能力构建的上层模块）                   │  │ │
│   │  │ ┌────────┐ ┌──────────┐ ┌──────┐ ┌─────────┐ ┌──────────┐ ┌─────┐│  │ │
│   │  │ │health  │ │snapshot  │ │web   │ │dfee     │ │exporter  │ │fault│ │  │ │
│   │  │ │健康度  │ │统一生产  │ │仪表盘│ │能效监控  │ │Prometheus│ │sub  │ │  │ │
│   │  │ │评估    │ │只读消费  │ │只读  │ │独立二进制│ │/metrics  │ │订阅  │ │  │ │
│   │  │ │        │ │边界      │ │消费  │ │只读消费  │ │:9100     │ │推送  │ │  │ │
│   │  │ └────────┘ └──────────┘ └──────┘ └─────────┘ └──────────┘ └─────┘│  │ │
│   │  │                                       ┌──────────────────┐          │  │ │
│   │  │                                       │ stragglerout     │          │  │ │
│   │  │                                       │ KPI 文件输出(opt)│          │  │ │
│   │  │                                       └──────────────────┘          │  │ │
│   │  └──────────────────────────────────────────────────────────────────────┘  │ │
│   │  ┌────────────────────────────────────────────────────────────────────────┐ │ │
│   │  │ internal/  config / metrics / storage / platform                       │ │ │
│   │  │           （配置 + 指标采集目录 + 数据存储 + 平台适配）                 │ │ │
│   │  ├────────────────────────────────────────────────────────────────────────┤ │ │
│   │  │ internal/collector  采集核心（Collector 接口 + Registry + Scheduler）   │ │ │
│   │  ├────────────────────────────────────────────────────────────────────────┤ │ │
│   │  │ internal/collectors  7 个部件采集器（cpu/mem/disk/gpu/npu/net/chassis）  │ │ │
│   │  ├────────────────────────────────────────────────────────────────────────┤ │ │
│   │  │ internal/source  来源层（14 包，抽象数据获取与解析）                      │ │ │
│   │  ├────────────────────────────────────────────────────────────────────────┤ │ │
│   │  │ Linux 系统接口（procfs/sysfs/syscall/exec） / Windows API              │ │ │
│   │  └────────────────────────────────────────────────────────────────────────┘ │ │
│   └────────────────────────────────────────────────────────────────────────────┘ │
│                                       ▲                                         │
│                                       │ vLLM OpenAI 兼容 API                    │
│                                       ▼                                         │
│   ┌────────────────────────────────────────────────────────────────────────────┐ │
│   │  vLLM 推理服务（vLLM-Ascend on NPU）                                       │ │
│   │   /v1/chat/completions · /v1/completions · 容错 REST /fault_tolerance/*    │ │
│   └────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────┘
                               ▲
                               │ DCMI / npu-smi / hccn_tool / nvidia-smi
                               │ /proc / /sys / ipmitool / smartctl ...
                       ┌──────┴──────┐
                       │ 服务器硬件   │  CPU/内存/硬盘/GPU/NPU/网卡/机箱
                       └─────────────┘
```

> accuracy-monitoring 作为 vLLM 进程内的 ASGI 中间件运行（不依赖 CATMonitor），直接拦截推理请求/响应做精度异常检测，并独立暴露 Prometheus 端点；其 WebUI 可独立部署聚合多实例。

### 2.2 核心设计原则

| 原则 | 说明 |
|------|------|
| 底座 + 特性分层 | 底座只做"采集 + 评估 + 输出/推送"，不感知上层特性业务；上层特性不重复造采集轮子，复用底座指标 |
| 模块独立 | CATMonitor、straggler 各自独立 `go.mod`，互不 import；EEP 为 Python + 补丁形态；accuracy-monitoring 为 Python ASGI 中间件包。耦合仅经文件/REST/Webhook/vLLM 插件契约 |
| 零侵入采集管道 | CATMonitor 新增输出/订阅/KPI 模块以 `collector.Storage` 插件形态接入（装饰器链），不改各采集器代码 |
| 极简依赖 | CATMonitor 仅依赖 `gopkg.in/yaml.v3`，推送/REST 用 Go 标准库 `net/http`；straggler 仅 `modernc.org/sqlite`（纯 Go，无 CGo）；EEP 用 Python 标准库 + `requests`/`ZMQ`/`msgspec`；accuracy-monitoring 用 `prometheus_client`/`pyyaml`/`numpy`/`httpx`/`colorlog` |
| opt-in 默认关闭 | CATMonitor 的 `faultsub`、`straggler_output` 等整合特性默认 `enabled: false`，不启用时底座行为零回归；accuracy-monitoring 经环境变量 `VLLM_ANOMALY_ENABLED` 开关 |
| 跨机部署 | CATMonitor 与 EEP/straggler 可分机部署，经回调 URL/REST 跨机通信；accuracy-monitoring 随 vLLM 进程内运行 |
| 跨平台 | CATMonitor 采集器经构建标签隔离 Linux/Windows 代码，NPU 全部指标 Linux 专属，无硬件时优雅降级 |

### 2.3 端到端数据流

```
服务器硬件
    │ DCMI/npu-smi/hccn_tool/nvidia-smi/procfs/sysfs/ipmitool/smartctl ...
    ▼
CATMonitor daemon
    │ Scheduler 按各 collector 周期触发 Collector.Collect() → []Metric
    ▼
metrics.Filter(allMetrics)            # 指标采集目录过滤（High/Medium + static + Feature-scoped 白名单）
    ▼
Storage 装饰链（按配置 opt-in 装配）：
    StragglerStorage(若启用)          # 抽取 NPU 11 项 KPI → 按时刻×按卡聚合 → 追加写 straggler_kpi_{date}.jsonl
    └ FaultStorage(若启用)           # 故障判定（card_drop/health/error_code/HBM UCE/RoCE 链路等） → FaultEvent
        └ CachingStorage              # 按组件分组原子替换内存缓存 → HTTP /metrics 读取转 Prometheus 文本
            └ JSONLStorage            # 落盘历史 {data_dir}/{component}_{date}.jsonl
    │
    ├── HTTP :9100 (exporter)         # GET /metrics、/-/healthy、/-/ready
    ├── HTTP :9101 (faultsub REST)   # 订阅注册/快照/事件查询 + 事件 ingest 端点（通用，外部检测器可回注）
    ├── faultsub Webhook              # 异步 POST FaultEvent 到订阅者 endpoint（EEP/运维）
    ├── snapshot.json + snapshot_<comp>.json  # web/dfee 只读消费
    └── straggler_kpi_{date}.jsonl   # straggler 读消费（一次性或 daemon 模式）
            │
            ▼
        straggler（一次性 或 --daemon 守护进程）
            # 一次性：读 KPI 最后聚合点 → 空间 peer 对比（kmeans）→ 报告
            # daemon：周期 dyno 触发采集 → python analyse 转 .db → 解析 → KPI+Profiler 检测 → 落盘 daemon_results/ + HTTP 查询/控制
            │ 命中慢卡（结果经 HTTP 接口或 straggler_output.json 消费；v0.2.3 起不再回注 faultsub）
            ▼
        EEP catmonitor_fault_sub.py    # （独立路径）接收 CATMonitor 硬件故障 webhook → NPU→DP 映射 → 调 vLLM REST API 下发容错指令
            ▼
        vLLM 容错框架（pause/scale_down/retry）  # 剩余健康 NPU 继续推理

    [vLLM 进程内] accuracy-monitoring ASGI 中间件
        # 透明拦截 /v1/chat/completions、/v1/completions → 强制注入 logprobs/top_logprobs/return_tokens_as_token_ids
        # → 抽取 (logprobs, token_ids) numpy 数组 → 响应恢复（resolver 优先还原 token 文本）→ fire-and-forget 调度 ILLDetector
        # → 检出 rare_character/garbled/repetition/nan_value → 独立 /anomaly/metrics + 异常落盘(pkl) + WebUI 告警
```

---

## 3. 底座 CATMonitor 设计

> 本节为 CATMonitor 底座的架构与模块设计摘要。完整设计见 [CATMonitor/DESIGN.md](../CATMonitor/DESIGN.md)。

### 3.1 分层架构

CATMonitor daemon 自下而上分为 6 层：

1. **系统接口层**：Linux procfs/sysfs/syscall/exec；Windows kernel32.dll/iphlpapi.dll/PowerShell。
2. **来源层 `internal/source/`（14 包）**：抽象数据获取与解析，采集器不直接 `os.ReadFile`/`exec`。返回 typed struct，带缓存（ipmi/dmesg/smartctl/hccn_tool）与可注入 fetcher，便于单元测试 mock。包括 `proc`/`sys`/`ipmi`/`lscpu`/`mce`/`dmesg`/`dmidecode`/`statfs`/`smartctl` + `dcmi`(CGo)/`npu_smi`/`hccn_tool`/`nvidia_smi`。
3. **采集器层 `internal/collectors/`**：7 个部件采集器（CPU/Memory/Disk/GPU/NPU/Network/Chassis），共享 `Collector` 接口 + 平台数据源分离（构建标签隔离）。
4. **采集核心 `internal/collector/`**：`Collector` 接口 + `Metric` 类型 + `Registry`（注册表）+ `Scheduler`（按周期定时调度 + Filter）。
5. **支撑层 `internal/config`、`internal/metrics`、`internal/storage`、`internal/platform`**：配置管理、指标采集目录（MetricSpec/Catalog/Filter + SetFeatureScope 白名单）、JSONL 数据存储、平台默认路径。
6. **特性层 `features/`**：基于采集基础能力构建的上层模块——`health`/`snapshot`/`web`/`dfee`/`exporter`/`faultsub`/`stragglerout`。

### 3.2 跨平台架构设计

核心策略：**共享逻辑 + 平台数据源分离**，通过 Go 构建标签在编译时选择。

```
collectors/{component}/
  ├── {component}.go         ← 共享：struct, Collect(), 指标定义, delta 逻辑
  ├── {component}_linux.go   ← Linux: 调用来源层(proc/sys/ipmi/...)采集
  ├── {component}_metrics.go ← 跨平台(无 build tag)：新增指标采集(来源报错→空)
  ├── {component}_windows.go ← Windows: kernel32.dll, PowerShell
  └── {component}_test.go    ← 测试 (//go:build linux)
```

关键原则：
- `Collector` 接口、`Metric` 结构体、健康度模块不感知平台差异。
- Linux 经来源层访问 `/proc`、`/sys`、`statfs`、`ipmitool` 等；Windows 直接 Go syscall 调 kernel32.dll/iphlpapi.dll，零第三方依赖。
- NPU 采集器平台分离：`npu_linux.go`（119 指标 device 并行 + DCMI CGo + npu_smi/hccn_tool）与 `npu_other.go`（`//go:build !linux` no-op stub），Windows 上整体降级跳过。
- DCMI CGo binding 在 `//go:build cgo && linux && dcmi` 之后，默认构建无 CGo，`-tags dcmi` 启用。

### 3.3 扩展机制：Collector 接口 + Registry 注册表

新增部件只需实现 `Collector` 接口并在 `init()` 中注册，调度引擎自动发现并调度，核心代码零修改。

```go
type Metric struct {
    Component  string            // 部件类型: "cpu", "memory", "disk"...
    Name       string            // 指标名称: "usage", "temperature"...
    Value      float64
    Unit       string            // "%", "MB", "rpm", "count"
    Labels     map[string]string // 设备号、核心号等
    Timestamp  time.Time
}

type Collector interface {
    Name() string
    Component() string
    Collect() ([]Metric, error)
    Priority() Priority             // High / Medium / Low
    DefaultInterval() time.Duration
    DefaultEnabled() bool
}
```

在 `main.go` 中通过 `import _ "catmonitor/internal/collectors/fpga"` 即可激活新采集器。

### 3.4 采集器与来源的依赖关系

| 采集器 | 依赖的来源包 | 指标数 | 说明 |
|--------|-------------|:------:|------|
| CPU | proc, sys, lscpu, mce, ipmi | 40 | usage/loadavg/温度/频率/拓扑/MCE/context_switches/process_count/model_info |
| Memory | proc, dmidecode, ipmi, dmesg | 19 | usage_detail/swap/PSI 饱和度/碎片化/DIMM/oom_count/power |
| Disk | proc, statfs, smartctl, dmesg | 7 | space_usage/iops/throughput/io_wait/io_errors/SMART + read/write_latency |
| GPU | nvidia_smi | 7 | utilization/memory_usage/temperature/power_draw/fan_speed/ecc_errors/clock_frequency |
| NPU | dcmi, npu_smi, hccn_tool | 119 | utilization/memory/temperature/power/health + 电压/风扇/13路温度/频率/利用率/HBM/ECC(delta)/LLC/带宽网络 + 45 项 hccn_tool 网络统计 |
| Network | proc, sys | 5 | throughput/packet_count/error_count/interface_status/connection_count |
| Chassis | ipmi | 5 | power/inlet_temp/outlet_temp/fan_speed/fan_power |

> 指标总数 204 项，详见 [CATMonitor/docs/CATMonitor_indi_list.md](../CATMonitor/docs/CATMonitor_indi_list.md)。

**NPU 采集器设计要点**：
- **device 并行采集**：collector 层每块 NPU 一个 goroutine，`WaitGroup` 等齐，单卡失败不影响其他卡；ECC delta 用 mutex 保护 `prevEcc` map。
- **两阶段**：Phase 1 `collectStatic`（全局/静态指标采 1 次：npu_num/comm_topo/driver_version/chip_type），Phase 2 `collectDevice`（每 device 一个 goroutine，采 119 指标）。
- **优雅降级**：`-tags dcmi` 未启用时（无 CANN SDK），DCMI `Available()=false`，所有 DCMI 方法返回 `errNotAvailable`，`Collect()` 不报错、仅输出非 DCMI 指标；无 NPU 硬件时输出 `npu_num=0`。

### 3.5 来源层设计

为解耦采集器与系统数据获取细节，引入 `internal/source/` 来源层。设计原则：

1. **parsed struct 返回**：来源返回 typed struct（如 `proc.CPUStat`、`proc.Meminfo`），采集器只做指标映射，不做字符串解析。
2. **单例 + 可注入**：来源包暴露单例访问点 + `SetRoot(path)`（重定向 `/proc`、`/sys` 测试根）+ 可注入 fetcher（测试时 mock exec）。
3. **缓存策略分档**：
   - **不缓存**：`proc`/`sys`/`statfs`（实时性要求高）。
   - **带 TTL 缓存**：`ipmi`(30s)、`dmesg`(30s)、`smartctl`(per-dev 60s)、`hccn_tool`(per-dev:opt 30s)。
   - **常驻缓存 (sync.Once)**：`lscpu`、`dmidecode`、`npu_smi.Topo`（拓扑静态，启动采集一次）。
4. **失败缓存（negative cache）**：`ipmi`/`dmesg`/`smartctl`/`hccn_tool` 无硬件或未安装时，失败结果也缓存，避免每周期重试 exec。
5. **跨平台降级**：`*_metrics.go` 为跨平台文件（无 build tag），Windows 上来源层不可用时返回空（优雅降级）。
6. **不建 Registry**：决策上暂不引入 `source.Registry` + list，采集器按需 import 来源包。

### 3.6 指标采集目录系统

为统一管控"采哪些指标、按什么优先级、默认是否采集"，引入 `internal/metrics` 指标采集目录。

设计要点：
1. **MetricSpec**：每个可采指标携带 `name/cn_name/priority(High|Medium|Low)/unit/static` 元数据；`static=true` 为一次性身份规格，默认采集。
2. **Catalog**：解析后的选择状态，按 `component → name → MetricSpec` 索引；从候选路径加载默认目录，无文件则空目录（默认放行全部）。
3. **模块覆盖**：模块自有 `metrics.yaml`（如 `features/health/metrics.yaml`、`features/dfee/metrics.yaml`）经 `LoadModuleOverride` 按 `name` 合并覆盖默认目录（模块值优先，缺省字段保留默认）。
4. **Filter（选择策略）**：`priority ∈ {High,Medium} OR static==true` 默认采集；Low 诊断指标默认不采。**目录中缺失的指标默认放行**（default-allow），避免目录漂移静默丢数据。
5. **DI 注入**：`scheduler.SetFilter(catalog.Filter)` 由 `cmd/catmonitor` 启动时装配。
6. **采集粒度预过滤**：`collection.min_priority`（low/medium/high）经 `metrics.SetCollectionThreshold` 设定阈值；`collector.SetWantedChecker(metrics.AnyWanted)` 把 `AnyWanted(component, names)` 注入采集核心。采集器在执行昂贵采集阶段前调用判断该指标组是否有任一指标通过阈值，无则整组跳过，降低无谓开销。
7. **Feature-scoped 白名单**：`catmonitor.yaml` 的 `features` 列表声明各特性所需指标。daemon 加载各 feature `metrics.yaml` 覆盖后，以 `SetFeatureScope(并集)` 建立白名单。`features` 非空时 `Filter` 只保留白名单内且 `priority ≥ min_priority` 的指标，`AnyWanted` 跳过产出全 out-of-scope 的子方法（不空跑硬件）；`features` 空 → 退回默认目录全集 + min_priority 预过滤。同时按 feature 声明的 interval 派生 per-component cadence `C_comp = min(声明该 comp 的 feature interval)`，`C_global = min(C_comp)`。

### 3.7 特性层模块概览

| 模块 | 形态 | 端口 | 职责 |
|------|------|:----:|------|
| `features/health` | 库 | — | 健康度评估：消费 `collector.Metric`，按部件评估器，权重自适应（含 GPU/NPU 时切加速卡方案），输出 0-100 健康分与等级。`catmonitor health` 子命令按需执行 |
| `features/snapshot` | 库 | — | Snapshot 统一生产：daemon 作为唯一 snapshot 生产者，产出 per-component `snapshot_<comp>.json` + 全局 `snapshot.json`（health/collectors/intervals/system_specs），原子写（临时文件 + `os.Rename`）。启动期 `CollectHWSpecs` 一次性采集跨部件身份 |
| `features/web` | 独立二进制 `catmonitor-web` | :9527 | Web 仪表盘：**只读消费** daemon 产出的 snapshot，可视化单机健康度与各部件指标。SPA + hash 路由，`//go:embed` 内嵌前端 |
| `features/dfee` | 独立二进制 `catmonitor-dfee` | :9528 | 能效监控：**只读消费** snapshot 渲染 25 张实时图表 SPA（CPU 8 jiffies→7 利用率推导 + 网络差值，卡片拖拽缩放、多选下拉筛选、模块折叠） |
| `features/exporter` | daemon 内置 | :9100 | Prometheus 导出：`CachingStorage` 包装 `JSONLStorage` 外，一次采集同时落盘 + 更新内存缓存（按组件分组原子替换），HTTP `/metrics` 转 Prometheus 文本（`catmonitor_{component}_{name}` 前缀，`_total`/`_time` 后缀判 counter） |
| `features/faultsub` | daemon 内置（opt-in） | :9101 | 故障订阅推送：`FaultStorage` 包装在 `CachingStorage` 外（实现 `collector.Storage`），对 NPU 指标做故障判定，经 HTTP Webhook 向已订阅者推送 `FaultEvent`，并提供订阅注册/快照/事件回补 REST API + 通用事件 ingest 端点。零新依赖（`net/http`），默认关闭 |
| `features/stragglerout` | daemon 内置（opt-in） | — | straggler KPI 输出：`StragglerStorage` 包装在最外层（实现 `collector.Storage`），仅处理 NPU 批次，抽取 11 项 KPI 按时刻×按卡聚合后追加写 `straggler_kpi_{date}.jsonl`，保留期 15 天。默认关闭 |

#### 3.7.1 健康度评估（`features/health`）

- **特性层模块**：仅消费 `collector.Metric`，不依赖任何采集器实现。
- **按部件评估器**：每个部件一个评估器文件（cpu/memory/disk/gpu/npu），规则就近定义，修改规则不影响采集逻辑。
- **局部 scheme**：`Evaluate` 使用局部权重方案、不改写 receiver；权重自适应——根据服务器是否含 GPU/NPU 自动选择。

| 场景 | CPU | Memory | Disk | GPU/NPU | 合计 |
|------|:---:|:------:|:----:|:-------:|:----:|
| 无 GPU/NPU | 30 | 40 | 30 | — | 100 |
| 有 GPU/NPU | 10 | 20 | 10 | 60 | 100 |

| 得分 | 等级 |
|------|------|
| 90-100 | Excellent |
| 75-89 | Good |
| 60-74 | Warning |
| 0-59 | Critical |

> 扣分规则与阈值见 [CATMonitor/features/health/HEALTH_SPEC.md](../CATMonitor/features/health/HEALTH_SPEC.md)。

#### 3.7.2 Snapshot 统一生产与只读消费（`features/snapshot`）

**解耦边界**：daemon 是 snapshot 的唯一写者（原子写：临时文件 + `os.Rename`）；web/dfee 是只读消费者，绝不调用采集器、不写本地文件。

**Snapshot 数据模型**：

| 文件 | 字段 |
|------|------|
| `snapshot_<comp>.json`（per-component） | `component`/`timestamp`/`metrics`/`history`(环形 60 点)/`specs`(启动身份规格) |
| `snapshot.json`（global） | `session_id`/`timestamp`/`refresh_interval_ms`(C_global)/`history_points`(60)/`health`/`collectors`/`intervals`/`system_specs` |

启动期 `CollectHWSpecs` 一次性采集跨部件身份（device_model/gpu_info/npu_info/disk_info/net_info/os_info），分发到对应 writer 的 specs。web `handleSnapshot` 合并 global + 各 per-comp 的 metrics/history/specs 组装响应。

#### 3.7.3 Prometheus 导出（`features/exporter`）

- **`CachingStorage`**：实现 `collector.Storage` 接口，包装在 `JSONLStorage` 外。一次采集同时：① 按组件分组更新内存缓存（原子替换，`AllMetrics()` 返回合并快照）；② 委托 `JSONLStorage.Write` 落盘历史。
- **`prometheus.go`**：`Encode(metrics) → Prometheus 文本`。命名 `catmonitor_{component}_{name}`（`-`/`/`/`.` → `_`）；`isCounter` 依据 `_total`/`_time` 后缀判 counter，其余 gauge；每组含 `# HELP` + `# TYPE`；标签按字典序排序。
- **端点**：`GET /metrics`（转 Prometheus 文本）、`GET /-/healthy`（200 OK）、`GET /-/ready`（缓存非空 200 / 否则 503）。

> 一次采集即同时落盘 JSONL + 缓存导出，不存在重复采集；HTTP 层只读内存缓存，绝不调用采集器。

#### 3.7.4 故障订阅推送（`features/faultsub`）

照 `features/exporter` 的"Storage 插件 + HTTP 端点"模式新建，daemon 导入即获得故障订阅能力，全部用 Go 标准库 `net/http`，不引入新依赖。

**核心组件**：

| 组件 | 职责 |
|------|------|
| `FaultStorage`（storage.go） | 实现 `collector.Storage`，包装 `CachingStorage`。每次 `Write` 既委托内层落盘/缓存，又把 NPU 批次 metrics 交给 `FaultDetector` 判定 |
| `FaultDetector`（detector.go） | 消费 `[]collector.Metric`，按规则产出 `FaultEvent`。纯 Go、不依赖 CGo，便于单测 |
| `Subscription` / `SubscriptionManager`（subscription.go） | 订阅表 + 去抖过滤。EEP 经 REST 注册"要什么、给谁、多久、怎么给" |
| `Dispatcher`（dispatcher.go） | 遍历匹配订阅，按 `DebounceMs` 抑制重复，按 `Delivery` 交付（webhook → 异步 POST；poll → 写入事件缓冲供 GET 拉取） |
| `Webhook`（webhook.go） | `net/http` 客户端，异步 POST `FaultEvent` JSON 到订阅者 `Endpoint`，失败重试 1 次，仍失败仅记日志（不阻塞采集管道） |
| REST API（server.go） | 独立端口 `:9101`（与 exporter `:9100` 解耦） |

**REST 订阅 API**：

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/faultsub/subscriptions` | 注册订阅，body 为 `Subscription`，返回 `{id}` |
| GET | `/faultsub/subscriptions` | 列出所有订阅 |
| GET | `/faultsub/subscriptions/{id}` | 查看单个订阅 |
| DELETE | `/faultsub/subscriptions/{id}` | 注销订阅 |
| GET | `/faultsub/snapshot` | 返回当前各 NPU 最新活跃故障快照（拉模式/调试） |
| GET | `/faultsub/events?since=&type=&npu_id=` | 查询近期故障事件（带过滤，供 poll/回补） |
| POST | `/faultsub/events` | **通用事件 ingest 端点**：外部检测器可回注命中事件，走与内部检测同样的分发/去抖/推送 |
| GET | `/faultsub/types` | 列出支持的故障类型 |
| GET | `/-/healthy` / `/-/ready` | 健康探针 |

**故障判定规则**：

| FaultType | 判定条件（基于已采集的 Metric） | Severity |
|-----------|--------------------------------|----------|
| `card_drop` | `card_drop` Value==1（新增指标）；或 `error_code` labels 含卡掉线码 `0x40f84e00` | critical |
| `npu_health` | `health_status` Value ∈ {2(Alarm),3(Critical)}；或 label `status` ∈ {Alarm,Critical} | warning→critical |
| `npu_error_code` | `error_code` Value>0（存在错误码）；detail 列出完整 hex 码 | warning |
| `hbm_uce` | `hbm_double_ecc` Value>0（本周期 delta） | critical |
| `ddr_uce` | `ddr_double_ecc` Value>0 | critical |
| `roce_link_down` | `roce_link_status` Value==0 或 label `status=="down"`；或 `roce_link_health` label 异常 | warning |
| `driver_unhealthy` | `driver_health` Value!=0 | warning |

`Detect(metrics)` 内部按 `npu_id` 分组，对每个 NPU 评估上述规则，产出 0..N 个 `FaultEvent`；同时检测"故障恢复"（上一周期有故障、本周期恢复）产出 `Recovered=true` 事件，供 EEP 决策"是否 retry"。

**FaultEvent 数据模型**：

```json
{
  "event_id": "a1b2c3d4-...",
  "type": "card_drop",
  "component": "npu",
  "npu_id": "3",
  "severity": "critical",
  "detail": { "error_codes": "0x40f84e00", "health": "Critical", "card_drop": "1" },
  "timestamp": "2026-07-28T10:30:00Z",
  "recovered": false
}
```

HTTP Webhook：`POST <endpoint>` 头 `Content-Type: application/json`、`X-CatMonitor-Event: <type>`，body 为上述 JSON。订阅者收到后回 `200`；非 2xx 或超时由 CATMonitor 按配置重试 1 次，仍失败仅记日志（不阻塞采集）。

> faultsub 的 `POST /faultsub/events` ingest 端点为通用能力，可供任意外部检测器回注命中事件。v0.2.3 起 straggler 自身不再使用该端点（改由 daemon HTTP 接口/结果文件消费命中结果）。

#### 3.7.5 straggler KPI 输出（`features/stragglerout`）

照 `features/faultsub`/`exporter` 的"Storage 插件"模式，新增 opt-in 模块 `stragglerout`，作为 `collector.Storage` 包装在最外层，仅处理 NPU 批次，按时刻×按卡聚合后追加写 KPI 文件。

**KPI 样本格式契约**（JSONL，每行一个时刻样本）：

```json
{"ts":1784547926,"vals":{"0":{"temp":47,"power":1628,"aicore_freq":1800,"aicore_util":45,"hbm_util":50,"tx_bandwidth":1250,"rx_pfc_pkt":0,"roce_tx_err_pkt":0,"roce_out_of_order":0,"roce_new_pkt_rty":0},"1":{...}},"cpu_avg":{"cpu1":"4.26"}}
```

文件名 `straggler_kpi_{date}.jsonl`，目录 `{data_dir}/straggler/`，保留期 `straggler_output.retention`（默认 360h=15 天）。内存缓冲 + 周期 flush（默认 60s），避免每 3s 重写文件。计数器指标写**原始累计值**，由 straggler 侧聚合时累加。

**指标映射表**：

| straggler 字段 | CATMonitor metric.Name | 来源 |
|---|---|---|
| temp | `temperature` | npu |
| power | `power_draw` | npu |
| aicore_freq | `aicore_freq` | npu |
| aicore_util | `utilization` | npu |
| hbm_util | `memory_usage` | npu |
| tx_bandwidth | `net_tx_bandwidth` | npu |
| rx_pfc_pkt | `mac_rx_pfc_pkt_num` | npu (hccn_tool) |
| roce_tx_err_pkt | `roce_tx_err_pkt_num` | npu (hccn_tool) |
| roce_out_of_order | `roce_out_of_order_num` | npu (hccn_tool) |
| roce_new_pkt_rty | `roce_new_pkt_rty`（底座新增，hccn_tool） | npu |
| cpu_avg | `usage` | cpu（按 cpu 标签聚合） |

### 3.8 命令行设计

```
catmonitor [command] [flags]
```

| 子命令 | 说明 |
|--------|------|
| `daemon` | 启动守护进程，持续周期采集指标并经 exporter 导出（v0.3.3 起不再周期评估健康度，改由 `health` 子命令按需执行） |
| `collect` | 单次采集所有指标，输出快照到标准输出或文件 |
| `health` | 基于当前指标执行一次健康检查，输出评估报告 |
| `list` | 列出所有已注册采集器及其指标清单 |
| `version` | 显示版本号、Go 版本 |

全局参数：`--config/-c`（配置文件路径，平台自适应）、`--output/-o`（`json`/`table`）、`--help/-h`。数据目录通过配置文件 `storage.data_dir` 调整，采集周期通过各 collector 的 `interval` 调整。

### 3.9 测试框架设计

- **每加一个指标，立即测试**：利用 Go 原生 `testing` + 表驱动测试。
- **无硬件也能测**：GPU/NPU 采集器在无硬件环境用 Mock 测试（`nvidia_smi.SetMock`、`dcmi.SetMockProvider`、`npu_smi.SetMock`、`hccn_tool.SetMock`）。
- **/proc /sys 模拟**：用 testdata 目录模拟 Linux procfs，保证测试可复现。
- 测试层级：单元测试（每采集器）/ 集成测试（多采集器协同）/ 健康度测试 / Mock 测试 / 端到端测试。

---

## 4. 上层特性 — EEP 推理卡级弹性容错

> 本节为 EEP 特性的架构与模块设计摘要。完整设计见 [feature/elastic-ep/DESIGN.md](../feature/elastic-ep/DESIGN.md)。

### 4.1 特性定位

EEP（Elastic EP）实现推理大 EP 部署的卡级弹性容错，目前仅支持 vLLM，后续计划支持 SGLang。在 DP（data parallel）+ EP（expert parallel）部署模式下，卡故障之后推理实例不退出，而是将故障卡所在的 DP 域隔离掉，重排专家后剩余 DP 继续提供推理服务；也支持网络闪断故障后请求重推恢复。

### 4.2 哨兵层级架构

容错框架采用 **三级哨兵架构**：

```
┌─────────────────────────────────────────────────────────────┐
│ 模拟外部故障管理中心 (scale_down_demo.py)                    │
│   - ZMQ SUB 订阅引擎健康状态（端口 22867，保留不变）           │
│   - HTTP Webhook 接收 CATMonitor 故障事件（catmonitor_fault_sub.py）│
└──────────────┬──────────────────────────────────────────────┘
               │ HTTP POST /fault_tolerance/apply
               ▼
        ClientSentinel（每个 vLLM 实例一个）
               │ ZMQ 分发 pause/retry/scale_down 指令
               ▼
   EngineCoreSentinel（每个 DP rank 一个）
               │ ZMQ 分发到 Worker
               ▼
   NPUWorkerSentinel（每个 NPU 工作进程一个）
               │ 执行 NPU 级操作
               ▼
        EngineCore (run_busy_loop) @fault_tolerant_wrapper
```

| 哨兵层级 | 数量 | 运行位置 | 职责 |
|---------|------|---------|------|
| **ClientSentinel**（顶层） | 每个 vLLM 实例一个 | API 服务器进程 | ZMQ ROUTER 接收所有 EngineCoreSentinel 的故障报告与哨兵注册；向外部系统发布引擎健康状态（ZMQ PUB）；向引擎分发容错指令；处理外部 REST API 请求（`/fault_tolerance/apply`、`/fault_tolerance/status`） |
| **EngineCoreSentinel**（中间层） | 每个 DP rank 一个 | EngineCore 进程 | 通过故障信号队列监控引擎异常；ZMQ 转发故障给 ClientSentinel；接收并执行 ClientSentinel 指令（暂停/重试/缩容）；与 WorkerSentinels 通信；执行重试清理流程（状态重置、Gloo 通信组重建） |
| **NPUWorkerSentinel**（底层） | 每个工作进程（NPU 设备）一个 | 工作进程 | ZMQ 接收 EngineCoreSentinel 命令；NPU 级执行暂停/重试/缩容；缩容中执行专家分布重算、专家权重重载、专家路由重建、并行参数更新、CPU Gloo 通信组重建、MC2 Mask 参数更新、MoE 配置更新等操作 |

### 4.3 容错工作流

#### 4.3.1 带外部故障管理中心（NPU 硬件故障场景）

```
故障检测阶段:
  NPU 硬件故障 (HBM UCE / 卡掉线)
    → CATMonitor 采集器(3s 周期) 采集并判定 → 生成 FaultEvent
    → HTTP Webhook 推送给 EEP catmonitor_fault_sub.py
    → EEP 映射 NPU→DP rank → 调 vLLM REST API
    → ClientSentinel 健康 DP rank 进入不健康状态
    → ClientSentinel 自动下发 pause 指令
    → EngineCoreSentinel 执行 pause，进入暂停状态
    → ClientSentinel ZMQ PUB 发布健康状态

故障响应阶段:
  外部故障管理中心 GET /fault_tolerance/status (轮询状态)
    → 返回引擎状态 (含 paused/dead)
  外部故障管理中心 POST /fault_tolerance/apply
    {instruction: scale_down, exclude_dp_ranks: [故障rank]}

缩容执行阶段:
  ClientSentinel → ZMQ 分发缩容指令 → EngineCoreSentinel → WorkerSentinel
    → 缩容助手 7 阶段执行：
      ① 专家分布重算
      ② 专家权重重载
      ③ 专家路由重建
      ④ 并行参数更新
      ⑤ CPU Gloo 通信组重建
      ⑥ MC2 Mask 参数更新
      ⑦ MoE 配置更新
    → Worker 上报完成 → EngineCore 上报恢复状态
    → ClientSentinel 发布新健康状态
```

#### 4.3.2 不带外部故障管理中心（手动响应场景）

```
故障捕获:
  WorkerSentinel 检测 NPU 异常 → ZMQ 故障上报
  EngineCoreSentinel fault_tolerant_wrapper 捕获引擎异常
  EngineCoreSentinel → ZMQ 故障上报 ClientSentinel
  ClientSentinel 健康 DP rank 进入不健康状态 → 自动下发 pause
  ClientSentinel ZMQ PUB 发布健康状态

等待指令 (最多 engine_recovery_timeout_sec):
  引擎暂停，等待容错指令

  alt 用户选择 retry:
    POST /fault_tolerance/apply {instruction: retry}
    → 清理状态 + 重建 Gloo 通信组 → 恢复请求处理

  else 用户选择 scale_down:
    POST /fault_tolerance/apply {instruction: scale_down, exclude_dp_ranks: [2]}
    → 缩容助手 7 阶段执行

  else 超时未操作:
    抛出原始异常，进程退出（优雅降级）
```

### 4.4 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 组件间通信 | ZMQ 套接字 | 低延迟高吞吐，解耦生产者-消费者，支持异步 |
| 有状态健康跟踪 | ClientSentinel 维护引擎状态字典（健康/不健康/已暂停/已终止） | 状态机清晰，便于外部决策 |
| 指令工作流模型 | ZMQ 消息（`pause`/`retry`/`scale_down` + 全局唯一 instruction_id + params） | 指令可追溯，分发到对应执行函数 |
| 故障上报机制 | ZMQ 故障报告消息（sentinel_id/pid/rank/err_type/err_msg/traceback） | ClientSentinel 记录后 PUB 广播健康状态 |
| 重试清理机制 | 清理状态 + 重建 Gloo 通信组 + 恢复请求处理 | 针对瞬时性错误 |
| 优雅降级 | `engine_recovery_timeout_sec` 超时重新抛出原始异常 | 系统可预测地失败而非无限期挂起 |

### 4.5 模块组成

| 模块 | 职责 |
|------|------|
| `patches/vllm_scale_down.patch` | vLLM v0.18.0 核心容错框架补丁（三级哨兵 + 容错框架） |
| `patches/vllm_ascend_scale_down.patch` | vllm-ascend v0.18.0 昇腾特定适配补丁 |
| `examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh` | 启动带容错功能的 vLLM 服务（模型 Qwen3-30B-A3-W8A8） |
| `examples/fault_tolerance_scale/scale_down_demo.py` | 外部故障管理中心，双路径故障检测（CATMonitor webhook 订阅 NPU 故障 + ZMQ 引擎健康订阅），检测到故障后下发容错命令（pause/scale_down/retry） |
| `examples/fault_tolerance_scale/catmonitor_fault_sub.py` | CATMonitor 故障订阅器：HTTP server 接收 webhook `FaultEvent`，NPU→DP 映射后下发 vLLM 容错指令 |

### 4.6 通信通道

| 通道 | 协议 | 方向 | 用途 |
|------|------|------|------|
| 引擎故障套接字 | ZMQ DEALER/ROUTER | 引擎 → ClientSentinel | 报告引擎异常（fault_report 消息） |
| 哨兵注册 | ZMQ DEALER/ROUTER | EngineCore → ClientSentinel | 启动时注册 sentinel_id/pid/rank 信息 |
| 故障状态 PUB/SUB | ZMQ PUB/SUB | ClientSentinel → 外部 | 广播引擎健康状态（health_status 消息） |
| 容错请求/结果 | ZMQ DEALER/PUSH | ClientSentinel → 引擎 | 向 EngineCore 分发 pause/retry/scale_down 指令 |
| Worker 进程命令 | ZMQ ROUTER/DEALER | EngineCore → Worker | 向 Worker 下发 pause/retry/scale_down 指令 |
| HTTP API | REST | 外部 → API 服务器 | 外部容错控制（`/fault_tolerance/apply`、`/fault_tolerance/status`） |

### 4.7 已测试模型与限制

- **已测试模型**：DeepSeek-V3（W8A8）、Qwen3-235B-A22B（W8A8）、GLM-5.1（W8A8）。
- **平台支持**：当前仅支持华为昇腾 A3 服务器。
- **框架支持**：当前仅支持 vLLM，后续计划支持 SGLang。

---

## 5. 上层特性 — Straggler 慢节点检测

> 本节为 straggler 特性的架构与模块设计摘要（v0.2.4/v0.2.3）。完整设计见 [feature/straggler/README.md](../feature/straggler/README.md)、[DESIGN.md](../feature/straggler/DESIGN.md)（Profiler 检测 + 守护进程）与 [DESIGN_NPU_RESOURCE.md](../feature/straggler/DESIGN_NPU_RESOURCE.md)（KPI 资源检测）。

### 5.1 特性定位

straggler 是 AI 智算集群中识别性能劣化 NPU 卡的**两道防线**检测体系。第一道 **KPI 资源检测**（轻量、常态化）基于 NPU 资源指标做空间 peer 对比（取最后一个聚合点）；第二道 **Profiling 深查**（按需触发）基于 Ascend PyTorch Profiler 数据从计算/通信/CPU/Bubble 四个维度精查。两道结果合并输出为**一个 JSON 文件**。既支持一次性手动运行，也支持**守护进程模式**（`--daemon`）常驻运行。

| 防线 | 包 | 输入 | 方法 | 输出 |
|------|----|------|------|------|
| **第一道（KPI 资源检测）** | `resource/` | KPI 时序（CATMonitor opt-in 输出 `straggler_kpi_{date}.jsonl`，取最后一个聚合点） | 空间 peer 对比（同节点卡互比，共享 kmeans 比例检测） | JSON + stdout 报告 |
| **第二道（Profiler 检测）** | `profiling/` | Ascend PyTorch Profiler `.db` SQLite（应用级、按需触发） | 均质化聚类：慢计算/慢通信/慢CPU（按物理节点 hostUid 分组）/NPU Bubble | JSON + 文本报告 |

**检测顺序**：先 KPI（轻量、无侵入）→ KPI 发现异常 → 有 `path` 时继续跑 Profiler 做交叉验证；KPI 无异常 → 自动 fallback 到 Profiler 精查；仅 KPI 无 `path` → KPI 结果即为最终输出。两道结果合并进 `straggler_output.json`（只跑哪个维度就只有哪个键）。

> **v0.2.3 重要变更**：① KPI 检测移除时间维度、历史基线、检测窗口、根因定界，异常**完全由空间维度 peer 对比**判定；② 新增守护进程模式 `--daemon`；③ 移除向 faultsub 回注 `straggler_detected` 事件的逻辑与 `--faultsub-url` 参数；④ 新增 `build.sh` 一键构建与 msmonitor 子模块；⑤ KPI/Profiler 空间检测统一走共享 `clustering` 包的 kmeans 比例算法。

> **v0.2.4 增量**：① daemon 新增 `POST /daemon/stop` 优雅关闭（关闭时删除全部 `daemon_results/` 归档结果）与 `--profiler-iterations` 采集迭代数参数（默认 1）；② 每周期 `op_metric/` 归档至 `daemon_results/<start>/` 并新增聚合视图接口 `/straggler/op_metric/*`；③ `clustering` score 统一为簇均值/基线均值（min 侧阈值取倒数）；④ 修复慢通信检测链路（`idToXp`/Duration 键空间错位）与并行拓扑去重失效；⑤ 移除 `--faultsub-url` 参数及 faultsub 回注（代码落地）。

### 5.2 目录结构

```
straggler/
├── main.go                 # 统一入口：CLI 解析、双模式编排、合并 JSON 输出、--daemon 入口（PATH 解析 dyno/dynolog）
├── daemon/                 # 守护进程：dynolog/dyno 采集 + 周期检测 + HTTP 查询/控制
│   ├── daemon.go           #   运行循环（周期调度、生命周期、优雅退出）
│   ├── dyno.go             #   dynolog 拉起 + dyno 触发校验 + python analyse 转 .db
│   ├── store.go            #   会话历史 + 周期计数
│   ├── server.go           #   HTTP 路由（/status /straggler/* /daemon/*）
│   └── types.go            #   Config / CycleResult / HTTP 响应类型
├── build.sh                # aarch64 一键构建（架构检查 + 装 dyno/dynolog(.deb) + Python wheel + Go 工具链 + go build）
├── go.mod / go.sum         # 独立 Go module（依赖 modernc.org/sqlite，无 CGo）
├── clustering/             # 共享 kmeans 比例检测算法（KPI 空间检测与 Profiler 均质化聚类共用）
│   └── kmeans.go
├── resource/               # 第一道防线：资源指标检测（KPI）
│   ├── types.go            #   数据结构 & 指标注册表 & 配置
│   ├── parser.go           #   CSV / KPI 目录解析（node 感知全局卡号）
│   ├── json_reader.go      #   CATMonitor straggler_kpi JSONL 读取（含多节点子目录布局）
│   ├── aggregator.go       #   10 秒聚合（裁剪均值 / 计数器增量）
│   ├── space_detector.go   #   空间维度检测（peer 对比，最后一点）
│   └── report.go           #   管线编排 + 文本报告（stdout）
├── profiling/              # 第二道防线：Profiling 检测
│   ├── dataparse/          #   数据清洗（SQLite → CSV/JSON 中间件）
│   └── detector/           #   检测算法（4 类检测 + 均质化聚类包装）
│       └── debug.go        #   --debug-output 诊断分数
├── utils/                  # 结果聚合（节点级）+ 工具
│   ├── node_result.go
│   └── tools.go
├── report/                 # Profiler 文本报告生成
├── DESIGN.md               # Profiling 检测设计（含守护进程模式）
├── DESIGN_NPU_RESOURCE.md # KPI 资源检测设计
├── SPEC.md                 # 检测技术规范
└── straggler_combination_DESIGN.md  # 与 CATMonitor 底座的整合设计
```

### 5.3 第一道：KPI 资源指标检测（`resource/`）

```
CSV/JSONL 解析 → 10 秒聚合 → 空间检测(最后一点 peer 对比) →
按指标分组异常卡(含空间劣化程度) → 合并 JSON
```

#### 5.3.1 输入数据

KPI 输入二选一，`--kpi-jsonl-dir` 优先于 `--kpi-path`：

- **`--kpi-path`（遗留 CSV 目录模式）**：目录内含多个每节点 CSV + 一个 `node_config.json`（映射 CSV → 物理节点 + 卡号）。CSV 每行一个时间戳，指标列值为 JSON dict（`{cardID: value}`）。
- **`--kpi-jsonl-dir`（CATMonitor 整合模式）**：读取目录内全部 `straggler_kpi_{date}.jsonl`（空间检测只取最后一个聚合点，历史数据用于 10 秒聚合）。支持两种布局：单节点平铺（文件直接放目录下）；多节点子目录 + `node_config.json`（key=子目录名，`node`=节点名，`cards`=实际使用卡号）。**一旦存在 `node_config.json` 就按多节点子目录读，顶层散放 jsonl 会被忽略**。

JSONL 单行记录：`{ "ts": <unix秒>, "vals": { "<cardID>": { "<字段>: <值>" } }, "cpu_avg": {...} }`，字段名用小写下划线（`temp`/`power`/`aicore_freq`/`aicore_util`/`hbm_bandwidth_util`/`hbm_util`/`tx_bandwidth`/`rx_pfc_pkt`/`roce_tx_err_pkt`/`roce_out_of_order`/`roce_new_pkt_rty`/`nic_rx_all_pkg`，其中 `nic_rx_all_pkg` 只采集不参与判定）。

#### 5.3.2 数据预处理：10 秒聚合

原始 KPI 采集频率高（3s），单采样点受瞬时波动影响大。`aggregator.go` 将 10 秒窗口内某卡某指标的原始采样点聚合为稳健统计量：

- **连续型指标**（temp/power/freq/util/bandwidth/nic_rx/cpu_avg）：**截尾均值（Midmean）**——排序后去前 25% 与后 25%，取中间 50% 的算术平均；桶内原始样本 < 4 时降级为普通均值；截尾后不足 2 点 → 中位数兜底。
- **计数型指标**（err_pkt/retry/out_of_order/pfc_pkt）：**增量累加**——取窗口增量（计数器回绕自动 `+= 2^64` 修正）。

空间检测只取**最后一个聚合点**，历史数据仅用于参与 10 秒聚合（不建基线、不设检测窗口）。

#### 5.3.3 空间维度检测（peer 对比，`space_detector.go`）

**peer 组 = 同一节点内的在场卡**（跨节点不互比）。对每个指标独立检测：

**指标注册表**（方法 = 空间检测方法；cluster 方向自适应，由双方向标记数对比决定，无需预判）：

| 指标 | 分类 | 空间方法 | 说明 |
|------|------|---------|------|
| `temp` | 计算 | cluster | 温度 (°C) |
| `power` | 计算 | cluster | 功耗 (W) |
| `aicore_freq` | 计算 | cluster | AI Core 频率 (MHz)，离散档位 |
| `aicore_util` | 计算 | cluster | AI Core 利用率 (%) |
| `hbm_bandwidth_util` | 计算 | cluster | HBM 带宽使用率 (%) |
| `hbm_util` | 计算 | cluster | HBM 内存使用率 (%) |
| `tx_bandwidth` | 通信 | cluster | TX 带宽 |
| `rx_pfc_pkt` | 通信 | absolute | PFC 暂停帧（计数） |
| `roce_tx_err_pkt` | 通信 | absolute | RoCE 发送错误包（计数） |
| `roce_out_of_order` | 通信 | absolute | RoCE 乱序包（计数） |
| `roce_new_pkt_rty` | 通信 | absolute | RoCE 重传包（计数） |

- **cluster（kmeans 比例）**：共享 `clustering` 包。≤0 读数（含真实 0）钳制到极小值 `zeroFloor=1e-3`——真实 0 是空闲/关闭读数，参与聚类而非丢弃 → z-score 标准化（std≈0 强制 1）→ 肘部法选 k → kmeans++ + Lloyd 迭代（固定种子，结果确定）→ **双方向各检一次**（max：基线 = 最小均值簇；min：基线 = 最大均值簇），各得标记集 α1 / α2 → **标记数少的方向为异常，个数相等不上报** → 对选中方向异常簇递归精化。参与聚类的卡都输出**真实簇比值**：基线簇成员恰为 1.0，其他未标记簇保留真实比值（如 1.2），被标记卡为其比值（> 阈值）；判定用选中方向递归 `Detect` 的标记（不随比值变化）。多卡同档异常会一起标记。方向无需预判：单卡降频、升温、冷却都能检出。
- **absolute**：错误计数类指标，值 `> 0` 即异常（sentinel 999）。

**判定与输出**：某指标某卡空间异常 → 该卡异常。输出按**指标分组**：每个异常指标下列出异常的卡及其 `score`（劣化程度 = 空间簇比例）。

#### 5.3.4 边界情况

| 场景 | 处理 |
|------|------|
| 空间维度同行点 < 2 卡 | 该节点 score=0（无法 peer 对比） |
| 某节点在场卡 < 2 | 该节点 score=0，其他节点不受影响 |
| ≤0 读数（含真实 0） | 钳制到 `zeroFloor=1e-3` 参与聚类（空闲/关闭读数不丢弃）；NaN 排除 |
| 缺失 / NaN 卡 | 该卡该指标 score=0（无读数，不参与聚类） |
| 裁尾后数据不足 | 降级为普通均值（桶内样本 < 4）；截尾后不足 2 点 → 中位数兜底 |
| 计数器回绕 | 自动加 `MaxUint64` 修正 |
| JSONL 某天文件不存在 | 天然跳过（只读存在的文件） |
| CSV 列不完整 | 缺失列告警但不阻断，对应 metric dict 为空 |
| 仅 KPI 无 `path` | 只输出 KPI 结果（`straggler_output.json` 只有 `kpi` 键），不执行 Profiler |
| KPI 检测失败（有 `path`） | 告警后继续执行 Profiler |
| `aicore_freq` 轻度降频（<2×） | 簇比例未超阈值 → 空间不标记（无时间维度兜底） |

### 5.4 第二道：Profiler 检测（`profiling/`）

```
SQLite .db → 并行域拓扑解析 → 单步快照 → 4 类检测 → 节点聚合 → 合并 JSON
```

#### 5.4.1 数据管线

```
Profiler .db（Ascend PyTorch Profiler 输出，每卡一个 SQLite 文件）
    │ DataParsing(folderPath) 遍历 .db → StartProcess
    │   信号量(cap=4) + WaitGroup，并发处理多个 .db
    ▼
ProcessDatabase(dbPath):
    1. sql.Open("sqlite", path+"?mode=ro") + WAL 模式
    2. 创建 3 个索引（IF NOT EXISTS，幂等）
    3. extractGlobalRankFromFilename → rank 字符串
    4. queryHostUid → 查询 HOST_INFO.hostUid（识别卡所属物理节点）
    5. readGroupInfo → META_DATA → group_info JSON（sync.Once 写入）+ xpToGroupName 映射
    6. GetAllStepTimes → 合并为单 step（minStart → maxEnd）
       3 级降级链：STEP_TIME 表 → TASK+STRING_IDS+MSTX_EVENTS（正则 step\d+）→ 哨兵
    7. TimeDiffForStep → 计算所有指标
    8. WriteResultsToCSV → 单行 CSV
    9. writeHostInfo → 写入 op_metric/host_info_{N}.json（rank → hostUid 映射）
```

#### 5.4.2 关键指标计算（TimeDiffForStep）

| 指标 | 计算方式 |
|------|---------|
| **ZP_Host** | 所有通信算子和 KERNEL_AICORE 的 `HEndNs - HStartNs` 均值（HStartNs > 0 && HEndNs ≥ HStartNs） |
| **ZP_Bubble** | 所有 `OpStartNs - HostEndNs > 0` 的正值均值 |
| **ZP_Duration** | 收集所有通信区间 → `mergeIntervalsSimple` 合并重叠 → 总跨度 |
| **ZP_Device** | `stepDuration - ZP_Duration`（钳位到 0） |
| **ZP_Kernel** | `SELECT AVG(endNs - startNs) FROM TASK ... WHERE KERNEL_AICORE` |
| 各域 Duration/Count | 域内算子 → `CalculateMidMeanPair`（去 min/max 后均值） |

#### 5.4.3 四类检测

| 类别 | 数据 | 阈值/方向 | 说明 |
|------|------|-----------|------|
| 慢计算 `cal` | ZP_Kernel（优先）/ ZP_Duration（降级） | `CalThreshold`(1+deg) | kmeans，方向 max/min |
| 慢通信 `comm` | `{域}_Duration` | `CommThreshold`(1+deg×5) | 每组取通信时长最小的卡为代表，按 PP stage 分桶后 kmeans，方向 max |
| 慢CPU `cpu` | ZP_Host（hostUid 平滑） | `CalThreshold` | 同主机卡取去 min/max 均值消除节点内差异 |
| Bubble `npu_bubble` | ZP_Bubble | `< 5000 ns` | 固定阈值直接判定 |

> cal / comm / cpu 三类检测统一走共享 `clustering` 包（kmeans 比例检测），与 KPI 空间 cluster 同一算法；Bubble 走固定阈值直接判定。

#### 5.4.4 主检测组优先级与去重

`tp → exp → ep → tp_exp → cp → cp2 → cp_ulysses → cp_ring → dp → dp_cp → dp_modulo_exp_cp`

并行域去重：`checkRankParallelExist` 通过 `parallelInfo map[int]map[int]bool` 追踪每个 rank 已归属的组，避免同域组重复。

#### 5.4.5 慢CPU 检测的节点分组

从每张卡的 `.db` 文件 `HOST_INFO` 表读取 `hostUid`，将相同 hostUid 的卡归为同一物理节点，节点内截尾均值（去 min/max）预处理后均质化聚类，消除节点内差异暴露节点间差异；`HOST_INFO` 表缺失的卡跳过预处理（降级兼容）。

### 5.5 共享聚类包 `clustering/kmeans.go`

KPI 空间检测与 Profiler 均质化聚类共用同一 kmeans 比例检测算法：

- z-score 标准化（std≈0 强制 1）→ 肘部法选 k → kmeans++ + Lloyd 迭代（固定种子，结果确定）→ 双方向各检一次（max/min）→ 标记数少的方向为异常簇 → 递归精化。
- 阈值：`SpaceRatioThreshold`（CLI `--space-ratio-threshold`，默认 2.0，独立旋钮，不随 degradation 变化）；Profiler 侧 `CalThreshold=1+degradation`、`CommThreshold=1+degradation×5`。
- `degradation`（默认 0.3）为灵敏度旋钮，联动 Profiler 阈值；`< 0` 重置为 0.3，`> 1` 允许但告警。

### 5.6 守护进程模式（`--daemon`，`daemon/` 包）

一次性模式按需手动运行；**守护进程模式（`--daemon`）常驻运行**，周期性自动完成「触发采集 → 转换 → 解析 → 检测」全链路，结果通过 HTTP 查询与运维控制，适合接入运维/调度系统持续巡检。

#### 5.6.1 工作原理

每到一个周期，daemon 自动执行一次完整的检测循环（每周期数据为 **`--profiler-dir` 根目录下的全部 rank 子目录**——dyno 每个 rank 写一个 `master_<pid>_<ts>_ascend_pt`，互不共享状态）：

```
dyno 触发采集 → 校验生效(commandStatus=effective + 命中 vllm 进程) → 等待 collect-wait →
对整个 --profiler-dir 根目录 python analyse 转 .db（覆盖所有 rank）→ dataparse 解析 →
KPI 检测(读 --kpi-dir) + Profiler 检测(整个根目录) → 合并 JSON + daemon_meta.json 直接落盘 daemon_results/<start>/ → 周期结束删除整个 profiler-dir
```

同时检测 **KPI 资源**与 **Profiler 深查**（未提供 `--kpi-dir` 时 KPI 段跳过，仅跑 Profiler），两者合并为一份 `straggler_output.json`。启动后等待一个周期（`--interval`）再开始循环；`POST /daemon/trigger` 可随时手动补跑一轮。每轮周期把结果直接落盘到 `--profiler-dir` 之外的 `daemon_results/<start>/`，周期结束时删除整个 `--profiler-dir`（dyno 下次采集自动重建）——存结果与删数据互不影响，防止 profiler 数据堆积。`Ctrl-C`/`SIGTERM` 优雅退出：停 HTTP、等当轮周期结束（≤10 分钟）、杀掉自己拉起的 dynolog、清理临时目录。

#### 5.6.2 前置条件与启动

| 条件 | 说明 |
|------|------|
| 硬件 | aarch64 Linux + Ascend NPU + CANN |
| Python | 3.9–3.12，装有 `torch_npu`（`build.sh` 自动安装 mindstudio_monitor wheel） |
| 采集链路 | 训练进程以 `MSMONITOR_USE_DAEMON=1` 启动（dyno 才能命中并触发采集） |
| 构建 | 先跑 `bash build.sh`（架构检查 + 下载 dyno/dynolog + 装 Python 依赖 + go build） |

```bash
cd feature/straggler
bash build.sh          # 首次构建
./slowNodeDetection --daemon \
    --profiler-dir=/data/profiler \   # 必填：采集落盘根目录（传给 dyno 的 --log-file）
    --kpi-dir=/var/lib/catmonitor/straggler \  # 可选：缺省则每轮只跑 Profiler
    --interval=600 \                  # 可选：检测周期（秒，≥60，默认 600）
    --collect-wait=60 \               # 可选：触发成功后等待采集完成的秒数
    --daemon-port=8080 \              # 可选：HTTP 端口（默认 8080）
    --degradation=0.3                 # 可选：灵敏度（与一次性模式同义）
```

`--profiler-dir` 必填；`--kpi-dir` 可选（缺省时每轮只跑 Profiler 检测，合并 JSON 不含 `kpi` 键）。`--kpi-dir` 与单次调用的 `--kpi-jsonl-dir` 共用同一读取逻辑（`resource.ReadKPIFiles`），支持平铺与多节点子目录两种布局。守护进程启动时会打印该目录可读取的 jsonl 文件数；若为 0 输出 WARNING。每轮周期的 KPI 执行结果（`ok`/`disabled`/`skipped: ...`/`failed: ...`）记录在 history 与 `/status` 的 `last_cycle.kpi_status` 中。

#### 5.6.3 HTTP 接口

路由**无 `/api/v1` 前缀**。查询类只读，控制类需 POST。

| 方法 & 路径 | 作用 | 请求体 |
|---|---|---|
| `GET /healthz` | 存活探针 | — |
| `GET /status` | 状态总览：state / interval_sec / 两个数据目录 / cycles_total / cycles_failed / last_cycle / next_run_at | — |
| `GET /straggler/results/latest` | 最近一轮合并结果 JSON（数据源 = `daemon_results` 归档文件） | — |
| `GET /straggler/results/history?limit=N` | 本次会话全部周期摘要（含失败的 error），按时间倒序；`?limit=N` 可选 | — |
| `GET /straggler/results/{id}` | 指定周期 id 的合并结果 JSON | — |
| `GET /straggler/report/latest` | 最近一轮 Profiler 文本报告（text/plain） | — |
| `GET /straggler/report/{id}` | 指定周期 id 的 Profiler 文本报告 | — |
| `GET /straggler/op_metric/latest` | 最近周期 op_metric 聚合视图（rank → group_info/host_info/op_metric 摘要） | — |
| `GET /straggler/op_metric/{id}` | 指定周期 op_metric 聚合视图 | — |
| `GET /straggler/op_metric/{id}/{file}` | 指定周期归档的单个原始 op_metric 文件（JSON/CSV） | — |
| `POST /daemon/start` | 恢复运行（paused → running） | — |
| `POST /daemon/pause` | 暂停（在跑的周期跑完，不再排新的） | — |
| `POST /daemon/interval` | 修改检测周期 | `{"interval_sec": 300}`（60–86400） |
| `POST /daemon/trigger` | 立即补跑一轮（已有周期在跑 → 409） | — |
| `POST /daemon/stop` | 优雅关闭守护进程（幂等）：关 HTTP server、等在跑周期结束、清理 dynolog 子进程，并**删除全部 `daemon_results/` 归档结果** | — |

#### 5.6.4 数据落盘与重启

```
<profiler-dir>/                    # 采集根目录；整个 profiler-dir 周期结束删除
├── master_<pid>_<ts>_ascend_pt/     # 每个 rank 一个子目录（dyno 落盘）
├── ascend_pytorch_profiler_*.db     # python analyse 转出的 SQLite（findDBs 递归发现）
├── op_metric/                       # dataparse 中间产物（写根下）
└── analysis_result/detection_report.log

daemon_results/<start>/           # 每轮结果直接落盘于此（归档记录；查询数据源）
├── straggler_output.json          # 本轮合并结果（latest/{id} 经 JSONPath 读）
├── daemon_meta.json               # 周期元数据（归档记录，查询不读）
├── op_metric/                     # 本轮 op_metric 中间产物归档（周期结束删 --profiler-dir 后仍可经 /straggler/op_metric/* 查询）
└── analysis_result/detection_report.log   # 文本报告（归档记录，report/latest 走内存）
```

**查询只看本次会话**：所有查询接口（latest/history/{id}/report）都从进程内 store 读，daemon 重启后清空，不读磁盘历史；本次会话的历史无条数上限，`/straggler/results/history` 默认返回全部周期（可用 `?limit=N` 截断）；`/status` 的 `cycles_total`/`cycles_failed` 同样是本进程内累计（重启归零）。

### 5.7 CLI 设计

```bash
# 一次性模式
slowNodeDetection [path=<profiler_dir>] [degradation=0.3] \
    [--kpi-path=<kpi_csv_dir> | --kpi-jsonl-dir=<jsonl_dir>] \
    [--space-ratio-threshold=2.0] [--debug-output]

# 守护进程模式
slowNodeDetection --daemon --profiler-dir=<dir> [--kpi-dir=<dir>] \
    [--daemon-port=8080] [--interval=600] [--collect-wait=60] \
    [--profiler-iterations=1] [degradation=0.3]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `path` | — | Profiler `.db` 目录（与 KPI 输入至少提供一个） |
| `degradation` | 0.3 | 灵敏度（<0 重置 0.3；>1 允许但告警；联动 Profiler 阈值） |
| `--kpi-path` | — | KPI 模式：每节点 CSV + `node_config.json` 的目录 |
| `--kpi-jsonl-dir` | — | KPI 模式：CATMonitor `straggler_kpi_{date}.jsonl` 目录（优先于 `--kpi-path`） |
| `--space-ratio-threshold` | 2.0 | 空间 kmeans 簇比例阈值（独立旋钮，不随 degradation 变化） |
| `--debug-output` | false | 全量数据排查（KPI 列出全部卡含正常 score；Profiler `node_result` 含所有节点） |
| `--daemon` | false | 进入常驻守护进程模式 |
| `--profiler-dir` | — | `--daemon` 时必填，采集落盘根目录（传给 dyno 的 `--log-file`） |
| `--kpi-dir` | — | daemon 模式 KPI 数据目录（缺省则每轮只跑 Profiler） |
| `--daemon-port` | 8080 | HTTP 端口 |
| `--interval` | 600 | 检测周期（秒，≥60，非法回退默认） |
| `--collect-wait` | 60 | dyno 触发成功后的等待秒数 |
| `--profiler-iterations` | 1 | 透传 dyno `nputrace` 的采集迭代步数 |

> `--baseline-hours`/`--detection-hours`/`--faultsub-url`/`--space-method`/`--space-z-threshold`/`--time-z-threshold`/`--time-weight`/`--no-trend`/`--no-fallback`/`--always-profiling` 等旧 flag 已移除。

### 5.8 构建与部署（`build.sh`）

`build.sh` 在 **aarch64 Linux** 主机上一次完成依赖与编译（仅支持 aarch64，其他架构直接报错退出）：

1. **架构检查**：`uname -m` != `aarch64` → 报错退出。
2. **安装 dyno / dynolog**：wget 下载 `dynolog_0.3.2_1.aarch64.deb`（msmonitor daily bucket）→ 检测系统包管理器（dpkg 原生安装 .deb；rpm 系需 `alien` 转换）→ 安装，使 `dyno`/`dynolog` 直接可从 PATH 调用（已安装则跳过）。
3. **Python 版本检查**：须 3.9/3.10/3.11/3.12，否则报错退出。
4. **装依赖**：下载并 `pip install` 对应 `mindstudio_monitor-26.2.0-cp<xx>-cp<xx>-linux_aarch64.whl`（cp 标签随 Python 版本）。
5. **Go 工具链**：需 ≥ go.mod 版本；缺失/过旧时从阿里云镜像下载并持久化 PATH（`/usr/local/go`，不可写时 `~/.local/go`）。
6. **编译**：`CGO_ENABLED=0 go build -o slowNodeDetection .`

产物 `./slowNodeDetection`。dyno/dynolog 由第 2 步装到**系统**（不进仓库，也不再使用 `3rdparty/` 嵌入）；下载的中间文件在临时目录，退出即清理。Go 编译**不再依赖** dyno/dynolog 二进制（已无 embed），任何平台都能出包；daemon 模式只在运行时要求目标 aarch64 主机装好 dyno/dynolog（跑一次 `build.sh` 即可）。

手动跨平台编译：

```bash
CGO_ENABLED=0 go build -o slowNodeDetection .                                   # aarch64（本机，daemon + 一次性都可用）
CGO_ENABLED=0 GOOS=linux  GOARCH=amd64 go build -o slownode_linux_amd64 .       # 跨平台，仅一次性模式
```

全静态二进制，无 CGo（Profiler 用纯 Go SQLite 驱动 `modernc.org/sqlite`）。测试：`go build ./... && go test ./...`。

### 5.9 输出与解读

两道检测结果合并为**一个文件** `straggler_output.json`（写在运行目录；daemon 模式另落盘 `daemon_results/<start>/`）：

```json
{
  "kpi": {
    "summary": { "total_cards": 8, "total_nodes": 2, "anomalies": 1, "normal": 7, "source": "...", "data_points": 129600, "space_ratio_threshold": 2.0 },
    "anomaly_metrics": [ { "metric": "aicore_freq", "method": "cluster", "cards": [ { "node": "node-1", "card_id": 0, "score": 2.25 } ] } ]
  },
  "profiler": {
    "node_result": [
      { "hostname": "<hostName>", "npu": [ { "id": 0, "cal": { "score": 1.5 }, "npu_bubble": { "score": 3200.0 } } ], "cpu": { "score": 1.4 } }
    ],
    "comm_domain_result": { "tp": { "0,1,2,3": 3.2 } }
  }
}
```

- **只跑 KPI** → 只有 `"kpi"` 键；**只跑 Profiler** → 只有 `"profiler"` 键；KPI 检测失败且无 Profiler → 不写文件。
- `kpi` 段：`summary`/`anomaly_metrics`（每个异常指标下列异常卡及空间 score）。
- `profiler` 段：`node_result[]` 按物理节点（hostname，缺失回退 hostUid）分组，`npu[]` 只含异常 NPU（cal/npu_bubble），`cpu` 节点级；`comm_domain_result` 按通信域分组。

文本报告：Profiler 报告 `path/analysis_result/detection_report.log`（4 类状态摘要表 + ZP_Kernel 跨 rank 排序柱状图 + ZP_Host 跨节点对比 + 通信域分组对比）；KPI 文本仅打印 stdout（`npu_resource_detection_report.log` 已移除）。

### 5.10 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| 1 | **KPI 仅空间维度 peer 对比** | 无需历史基线/检测窗口，kmeans 无需历史噪声尺度；唯一旋钮 `SpaceRatioThreshold`；轻量、常态化、覆盖硬件层异常 |
| 2 | **KPI 与 Profiler 共享 kmeans 比例算法** | 统一检测语义（簇均值/基线均值，双方向各检一次取标记少的），减少重复实现 |
| 3 | **守护进程模式（daemon）** | 周期自动采集+检测+HTTP 控制，适合接入手管/调度系统持续巡检；与一次性模式共用同一检测管线（`detectFromParsedData` 以 `DetectFunc` 注入 daemon，避免 import cycle） |
| 4 | **采集工具走系统安装而非 embed** | dyno/dynolog 由 build.sh 装到系统 PATH，仓库不携带第三方制品；Go 产物与采集工具解耦，任何平台可出包 |
| 5 | **结果落盘 + 周期删 profiler-dir** | 结果落 `daemon_results/<start>/`（profiler-dir 之外），周期结束删整个 profiler-dir，防堆积，互不影响 |
| 6 | **查询只看本次会话** | 进程内 store 读，重启清空；避免跨重启状态不一致 |
| 7 | **移除 faultsub 回注** | 与守护进程结果上报重复，命中慢卡改由 daemon HTTP 接口/结果文件消费 |
| 8 | **KPI 优先 + Profiling 降级** | KPI 无侵入覆盖硬件层，Profiling 按需覆盖软件层，两者互补 |

---

## 6. 上层特性 — 推理精度异常检测（accuracy-monitoring）

> 本节为 accuracy-monitoring 特性的架构与模块设计摘要。完整设计见 [feature/accuracy-monitoring/design.md](../feature/accuracy-monitoring/design.md) 与 [README.md](../feature/accuracy-monitoring/README.md)。

### 6.1 特性定位

推理精度异常检测基于模型输出的 token 与 logprobs 序列，在**无侵入、零参照知识**的条件下，实时检测推理过程中可能出现的异常响应，对 GenAI 推理服务中的生僻字、乱码、重复输出、NaN 值等**输出崩溃类故障**做在线实时、高准确率检测：

- **生僻字**（rare_character, ill_type=1）：偶发性输出无意义字符，不符合上下文语境。
- **乱码**（garbled, ill_type=2）：模型持续输出生僻字，文本无意义，无法正常对话。
- **重复**（repetition, ill_type=3）：重复输出相同内容。
- **NaN Value**（nan_value, ill_type=4）：logprobs 出现 nan/inf 值。

通过 vLLM `--middleware` 插件部署（`vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware`），作为纯 ASGI 中间件透明拦截推理请求，强制采集 logprobs 与 token_id，后台运行异常检测算法，并通过独立 Prometheus 端点暴露检测结果。**全过程对客户端无感知**——不影响响应状态、不阻塞响应返回、不泄漏内部参数。

### 6.2 设计原则

- **透明优先**：`enabled=True` 与异常监控概率共同决定请求是否注入/恢复/检测，但不影响客户端看到的响应。
- **流式不缓冲破坏**：采用纯 ASGI（而非 Starlette `BaseHTTPMiddleware`），SSE 流增量转发，避免缓冲破坏流式语义。
- **检测不阻塞客户端**：检测在响应全部发送完毕后以 fire-and-forget 方式调度，客户端永远不等待检测。
- **硬依赖启动报错**：`configs/detector.yaml`、env 变量、ILLDetector 构造、tokenizer 加载等启动期硬依赖失败 → 终止服务启动（fail-fast），避免"静默降级运行"。
- **推理期降级 / 检测优先**：检测执行异常、单 token decode、进程池崩溃等推理期错误 → log + 不影响推理 + 不降级算法、不设 `enabled=False`。
- **不改写检测算法逻辑**：检测器接口从 `List[Dict[int,float]]` 改为 numpy 数组，仅改数据访问方式，阈值/FFT/滑窗/n-gram 等算法逻辑完全不变。

### 6.3 模块组成

```
accuracy-monitoring/
├── pyproject.toml           # 项目配置（包名 anomaly_middleware）
├── conftest.py              # pytest 根配置（sys.path 设置）
├── configs/
│   ├── detector.yaml        # 检测器算法默认参数
│   └── webui.yaml           # Web 信息配置
├── tests/                   # 单元测试 + 端到端测试
├── webui/                   # Web 精度可视化监控
└── anomaly_middleware/      # Python 包
    ├── __init__.py          # 重导出 AnomalyMiddleware / ResponseInterceptor / RequestContext
    ├── middleware.py        # 统一中间件类 + RequestContext + ResponseInterceptor + eager 初始化
    ├── env.py               # 环境变量处理（PluginConfig）
    ├── logging.py           # 日志格式
    ├── metrics.py           # 独立 CollectorRegistry + 指标记录/渲染
    ├── extractor.py         # 抽取/恢复（流式与非流式）+ SSEStreamProcessor
    ├── token_resolver.py    # TokenTextResolver + tokenizer 获取 + parse_vllm_argv
    ├── token_categorizer.py # token 分类纯函数 + 启动期 generate_tk2cat
    ├── detector.py          # ILLDetector 检测器本体（set_vocabulary + topk_n 参数）
    ├── detector_runner.py   # DetectorRunner（进程池 + 共享内存 + 调度 + 词表注入）
    └── anomaly_store.py     # 异常信息本地保存（编号分配 + pickle 落盘）
```

### 6.4 部署形态与四条硬约束

`--middleware <module.path>.<ClassName>` 由 vLLM 经 `importlib`/`getattr`（点分隔）加载，因值为类而实例化为 `Cls(app)`——**无 kwargs、无启动钩子、无路由注册钩子**。由此导出四条硬约束：

1. 构造签名固定为 `__init__(self, app)`；
2. 全部配置来自环境变量/磁盘文件；
3. 重活（numpy、tokenizer、检测器、进程池）须在 `__init__` 同步完成——`__init__` 即唯一初始化时机，启动期 fail-fast；
4. 指标端点必须由中间件**内联**响应（不能用 `app.add_api_route`）。

### 6.5 请求拦截与参数注入

**拦截范围**：仅 `/v1/chat/completions` 与 `/v1/completions`，其他 HTTP 请求原样转发。

**请求侧 ASGI 契约**：读请求体（聚合全部 `http.request` body）→ 重放 receive（首次返回合成单条 body，后续委托原始 receive，**绝不返回空 body** 避免重复处理）→ 浅拷贝 scope 改写 `content-length`。

**强制注入**（覆盖请求体加检测所需参数）：
- chat：`logprobs=true`、`top_logprobs=<注入值>`、`return_tokens_as_token_ids=true`
- completions：`logprobs=<注入值>`、`return_tokens_as_token_ids=true`
- **注入值 = max(客户端原始值, N)**（N=`VLLM_ANOMALY_TOP_LOGPROBS`，默认 20，范围 1-20），保证每 token 有足够 top-logprobs 数据。
- `return_tokens_as_token_ids` 始终注入 `true`（使响应 token 字段呈 `"token_id:NNN"` 供检测抽取）；客户端未带 → 恢复时还原其默认。

注入前快照客户端原始参数（`logprobs`/`top_logprobs`/`logprobs`/`return_tokens_as_token_ids`/`n`）供响应恢复；注入后修正请求 `Content-Length` 匹配新 body 长度。

**关键不变量**：`top_logprobs` 跨请求恒定（默认 20，可配置 1-20），保证每 token 的 top-logprobs 条目数一致，检测语义稳定。

### 6.6 响应抽取与恢复

**抽取**（供检测）：每个 choice 取 `(logprobs: np.ndarray, token_ids: np.ndarray)` per-choice numpy 数组；内部遍历 JSON 收集 token_id + logprob，`np.argsort(kind='stable')` 排序后截断，产出**已降序排列**的数组。

**恢复**（供客户端，按原始参数，统一走 `_token_text` 规则）：
- 客户端未请求 `logprobs` → `choice.logprobs=null`。
- 客户端请求 `logprobs=True`、`top_logprobs=n` → 截断到 n。
- 客户端未请求 `return_tokens_as_token_ids` → token_id→文本还原（**resolver 优先** `decode([id])`，个别 decode 失败退回 bytes，带 `token_id:` 前缀守卫，再无则 null）。
- 客户端**已**请求 `return_tokens_as_token_ids` → 原样保留 `token_id:NNN`。

> vLLM 在 `return_tokens_as_token_ids=true` 下，chat `top_logprobs` 的 `bytes` 填的是 token_id 字符串本身的字节（非 token 真实字节）会泄漏 `token_id:`；completions 响应无 `bytes`。故引入 tokenizer `decode([id])` 为统一文本来源（`TokenTextResolver`，启动期硬依赖）。

**检测截断与客户端截断分离**：注入值 `max(客户端, N)` 时每 token top-logprobs 条目数可能 > N；送检测截前 N 项，返回客户端截至客户端请求值。

### 6.7 流式响应处理（SSE）

设计要求**先缓存全部流式推理结果，再进行检测**（vLLM 流式每块只含最新 token 的 logprobs/token_id）。

**SSEStreamProcessor 状态机**：跨块缓冲 `_buffer`，按 `\n\n` 切分完整事件；半事件留缓冲。`_process_event` 分离 `data:` 行与其他行；keep-alive/`data: [DONE]` 原样透传；其余 `json.loads` 成功则捕获 model → `_extract_streaming`（per-choice append 累积）→ `_strip_streaming`（每块无状态恢复）→ 重序列化。`flush()` 排空尾部残余。

**双态并存**：转发是增量无状态的（每块即发），检测数据累积是有状态的（跨块 append），两者读写不同字段互不干扰。CRLF 兼容（`\r\n\r\n`）。

### 6.8 异常检测调度（fire-and-forget）

- 检测在**响应全部发送完毕后**调度：非流式在终端 body 发出后；流式在 `[DONE]`/`more_body=False` 后。
- 检测数据：非流式取 `extract_*_response` 的 per-choice 数组；流式取 `SSEStreamProcessor.get_detection_data()`。
- **空响应不检测**（`token_ids` 为空或全空跳过）。
- 检测任务防 GC：中间件持 `_pending_tasks: set`，入集，`done_callback` 出集；关闭时未完成任务随 event loop 取消。
- 异常全捕获：检测协程内 try/except，失败计 `detection_errors_total`，不影响客户端。

### 6.9 异常监控概率与请求关联标识

- **异常监控概率**（`VLLM_ANOMALY_MONITOR_RATE`，默认 1.0，范围 0-1）：每目标请求抽 `rand`；`rand < monitor_rate` 选中走完整注入/恢复/检测链路；未选中 → **纯透传**（不读 body、不注入、不恢复、不检测）。`0` 永不检测，`1.0` 全检测。
- **运行时动态调整**：`monitor_rate` 可经动态配置端点在运行时更新（`POST /anomaly/config`，见 §6.11.1），即时生效、不持久化（重启回退 env 初始值）；其余配置仍为启动期不可变。
- **请求关联标识**：每被拦截请求生成 `request_id = uuid.uuid4().hex`；在 `http.response.start` 追加响应头 `x-anomaly-request-id` 后再发给下游，支持端到端追踪。

### 6.10 Prometheus 指标

`__call__` 在最前拦截 `GET <metrics_path>`（默认 `/anomaly/metrics`）内联响应，独立 `CollectorRegistry`（与 vLLM 默认 `/metrics` 隔离）。仅 GET 被拦截；POST 透传。

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `vllm_anomaly_requests_total` | Counter | — | 被检测请求计数 |
| `vllm_anomaly_detected_total` | Counter | `ill_type`, `model` | 检出异常计数 |
| `vllm_anomaly_detection_errors_total` | Counter | — | 检测失败计数 |
| `vllm_anomaly_detection_duration_seconds` | Histogram | — | 检测耗时 |
| `vllm_anomaly_last_rare_character` | Gauge | `model` | 最近生僻字结果（ill_type=1） |
| `vllm_anomaly_last_garbled` | Gauge | `model` | 最近乱码结果（ill_type=2） |
| `vllm_anomaly_last_repetition` | Gauge | `model` | 最近重复结果（ill_type=3） |
| `vllm_anomaly_last_nan_value` | Gauge | `model` | 最近 NaN 结果（ill_type=4） |
| `vllm_anomaly_monitor_rate` | Gauge | — | 当前异常监控概率（运行时经 `/anomaly/config` 可更新） |

`ill_type` 取值：`0`=normal, `1`=rare_character, `2`=garbled, `3`=repetition, `4`=nan_value（normal 只增 requests，不计 detected）。`model` 标签来自请求体 `model` 字段，缺失用 `"unknown"`。

### 6.11 关键组件设计

#### 6.11.1 AnomalyMiddleware（统一中间件类）

`__init__(self, app)`：建 `PluginConfig`（`from_env`，env 越界 → raise）；attach 指标助手；建 `_pending_tasks` set。`enabled=True` 则**同步**完成 eager 初始化（任一步骤失败直接 raise 终止启动）：

1. `resolve_config_path()` → 检查 `configs/detector.yaml` 存在；不存在 raise。
2. 加载 tokenizer（同步，路径顺序见 §6.11.7）→ 构造 `TokenTextResolver`。
3. `generate_tk2cat(tokenizer)` 生成词表映射；失败 → 软降级（tk2cat=None，无词表检测，记 WARNING）。
4. eager 构造 `ILLDetector(config_path)`（主进程，验证 numpy 可用 + config 解析）；成功 → 丢弃实例（仅验证用）。
5. 构造 `DetectorRunner(config_path, max_workers, topk_n, tk2cat, vocab_size)`（含 `ProcessPoolExecutor`，initializer 注入 tk2cat）。

`enabled=False` → 跳过全部 eager 初始化（纯透传，指标端点仍可达报零值）。`__call__` 分派：非 http scope 透传；`GET <metrics_path>` 内联响应；动态配置端点（`GET/POST <config_path>`，默认 `/anomaly/config`，路径 `VLLM_ANOMALY_CONFIG_PATH` 可配）内联响应——GET 返回当前 `{"monitor_rate": <float>}`，POST 校验 ∈ [0.0, 1.0] 后更新 `self._monitor_rate` 并同步 gauge（校验失败 400 且当前值不变；其他 method 透传；无认证、不持久化、`enabled=False` 仍可达）；非 POST/非目标路径/`enabled=False` 透传；异常监控概率未选中纯透传；选中走读 body→注入→恢复→检测链路。

#### 6.11.2 ResponseInterceptor（响应拦截器）

判 `content-type` 含 `text/event-stream` → 流式建 `SSEStreamProcessor` 立即 send(start)，每块 `feed` 增量 strip+转发同时累积检测数据；非流式缓冲到 `more_body=False` 后 `extract_*`+`strip_*`+重序列化发送。响应发送完毕后 `_maybe_schedule_detection` 调度检测。

#### 6.11.3 DetectorRunner（进程池 + 共享内存零拷贝）

`ProcessPoolExecutor` + 共享内存零拷贝。`max_workers` 来自 `VLLM_ANOMALY_DETECTOR_WORKERS`（默认 4，建议 1-16）。建池时经 `initializer=_worker_init`、`initargs=(config_path, tk2cat, vocab_size, topk_n)` 注入 tk2cat 到每进程 worker。

**模块级函数**（须可 pickle）：`_worker_init`（每进程构造 `ILLDetector` + `set_vocabulary`）；`_detect_sync(metadata)`（从 `SharedMemory` 零拷贝重建 `logprobs`/`token_ids` numpy 数组 → 逐候选 `det.detector(logprobs, token_ids, topk_n)` → 返回 `[[is_ill, ill_type]]`）。

**`schedule_detection`**：分配 SharedMemory + 写入两数组 + 构造元数据（`shm_name`/`num_choices`/`topk_n`/`choice_lengths`/`shapes`/`offsets`，变长候选按最长 padding）→ `asyncio.create_task(_run_detection)`。`_run_detection` 计时 → `runner.run_async(metadata)` → `record_detection`；`except BrokenProcessPool` → `runner._rebuild_pool()` + 计 error + log；`except Exception` → 计 error + log。最后 `_cleanup_shm`。

#### 6.11.4 ILLDetector（检测器本体）

- **vendored 含义**：`detector.py` 是检测算法源码被直接内置进本项目包，随中间件分发；`configs/detector.yaml` 是其算法默认参数（窗口大小、各类异常阈值）。
- **构造** `ILLDetector(config_path)`：仅加载 `detector.yaml`，无模型识别/预生成映射文件依赖。
- **`set_vocabulary(tk2cat, vocab_size)`**：接受启动期生成的 `{str(token_id): category}` 映射；幂等，重复调用覆盖；经 `ProcessPoolExecutor` initializer 在每进程构造检测器后同步注入。
- **`topk_n` 参数**：`run(logprobs, token_ids, topk_n=N)` 由参数传入 topk 截断值，消除实例态 `topk` 首次锁定问题。
- **输入格式**：`logprobs: shape=(num_tokens, topk_n), float32, 已降序排列`；`token_ids: shape=(num_tokens, topk_n), int32, 与 logprobs 同序`；内部取 `token_ids[:, 0]` 作为输出 token 序列。
- **无词表降级**：`tk2cat` 为 `None` 时 rare/garbled 走 top1 logp 路径（按概率阈值判异常），repetition/acf/trajectory 不受影响。
- 检测算法含生僻字（rare_character）、乱码（garbled，含 FFT）、重复（repetition，trajectory + acf 自相关 + 滑窗）、NaN 值四类，阈值见 `detector.yaml`。

#### 6.11.5 TokenTextResolver（token 文本还原）

`resolve(token_id) -> Optional[str]`：给定 token_id 返回该 token 的 surface 文本（OpenAI 语义即 `decode([id])`）。进程级单例、启动期同步构造、全请求复用；`resolve` 内部维护 `dict[int, str]` 缓存（首次 `decode([id])` 后存入）。仅被 ASGI 事件循环（strip 路径）调用；检测 worker 进程不调用（用 token_id 整数）。加载失败 → raise 终止服务启动（启动期硬依赖，不软降级）。

#### 6.11.6 token 分类与词表注入

`token_categorizer.py` `categorize_token`：对单个 token 的解码文本逐字符做 Unicode 脚本分类，统计各类别占比，取主导类别映射为标签（如 CJK→`chinese_cjk`、拉丁→`english_latin`、数字→`numbers`、符号密集→`gibberish_symbols`、控制字节→`control_bytes` 等）。`generate_tk2cat(tokenizer)` 在启动期从已加载 tokenizer 同步生成 `{str(token_id): category}` 映射，经 `ProcessPoolExecutor` initializer 注入每进程 worker；生成失败 → 软降级（无词表检测模式）。

#### 6.11.7 tokenizer 获取顺序

启动期 `acquire_tokenizer(explicit)`（同步，不含 HTTP loopback）：

1. **显式 env `VLLM_ANOMALY_TOKENIZER_MODEL`**（最高优先）：本地目录路径或 HF repo id → `from_pretrained(..., local_files_only=True)`。
2. **`--tokenizer` 从 `sys.argv` 解析**（`parse_vllm_argv()`）：vLLM 启动命令中 `--tokenizer <path>`。
3. **`--model` 位置参数从 `sys.argv` 解析**：无 `--tokenizer` 时，`vllm serve <model>` 的 `<model>`。
4. **HF 缓存扫描**：以 argv `--model` 名为 hint，`huggingface_hub.scan_cache_dir()` 找 `repo_id` 以 `/<hint>` 结尾或等于 `<hint>` 的条目。
5. 均失败 → raise（终止服务启动），提示设置 `VLLM_ANOMALY_TOKENIZER_MODEL`。

### 6.12 异常信息本地保存（`anomaly_store.py`）

当某被检测请求的某候选检出异常时，把异常现场保存为本地 pickle 文件（dict，key=异常编号，value=`time`/`prompt`/`ill_type`/`topk_logprobs`/`tokens_ids`/`text`/`model_name`/`text_tokenid`（per-choice 实际生成 token_id 序列，非流式与流式 SSE 累积均可）/`reasoning_content`（chat 思维链文本，非 chat 或无则为 null））。`VLLM_ANOMALY_SAVE_PATH` 控制：未设不落盘（异常编号仍由内存计数器累加，重启归零）；以 `.pkl` 结尾→文件模式；否则→文件夹模式（文件名=`<served_model_name>.pkl`）。启动期 fail-fast 校验目录/父目录存在性；磁盘写经 `loop.run_in_executor` offload 到线程，`asyncio.Lock` 串行化，事件循环不阻塞；保存失败 → catch + log，不影响客户端/检测/后续请求。

### 6.13 环境变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `VLLM_ANOMALY_ENABLED` | `1` | 总开关，`0`/`false` → 纯透传不检测 |
| `VLLM_ANOMALY_MONITOR_RATE` | `1.0` | 请求被异常监控的概率，0-1 |
| `VLLM_ANOMALY_TOP_LOGPROBS` | `20` | 注入的 top-logprobs 数量，1-20 |
| `VLLM_ANOMALY_DETECTOR_WORKERS` | `4` | 检测进程池 worker 数，≥1 |
| `VLLM_ANOMALY_METRICS_PATH` | `/anomaly/metrics` | 指标端点路径 |
| `VLLM_ANOMALY_CONFIG_PATH` | `/anomaly/config` | 动态配置端点路径（GET 查询 / POST 更新 `monitor_rate`） |
| `VLLM_ANOMALY_TOKENIZER_MODEL` | None | 显式 tokenizer 加载源（最高优先） |
| `VLLM_ANOMALY_SAVE_PATH` | None | 异常详细数据本地保存路径（pkl） |

### 6.14 推理精度异常监控 Web 界面（`webui/`）

独立的 Web 服务，支持多 vLLM 实例聚合可视化推理精度异常检测现象，并支持可配置的阈值告警和多渠道告警（界面告警 + Webhook（钉钉、飞书、企业微信）+ 邮箱通知）。v0.2.4 增强：新增历史数据导入（`POST /api/import`，将实例异常 pickle 落盘转换为阶梯趋势事件并入累计统计，可按实例清除）、趋势保留延长至 30 天（`trend_horizon_seconds` 默认 2592000）、环形缓冲默认容量调整（事件 1000 / 告警 200）。详见 [feature/accuracy-monitoring/webui_README.md](../feature/accuracy-monitoring/webui_README.md)。

### 6.15 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| 1 | **纯 ASGI 而非 BaseHTTPMiddleware** | SSE 流增量转发，避免缓冲破坏流式语义 |
| 2 | **检测 fire-and-forget** | 客户端永远不等待检测；检测在响应发送完毕后调度 |
| 3 | **进程池 + 共享内存零拷贝** | numpy 数组零拷贝传检测 worker，GIL 不阻塞事件循环；`BrokenProcessPool` 自动重建 |
| 4 | **启动期 fail-fast（硬依赖）** | detector.yaml/env/tokenizer/检测器构造失败即终止启动，避免静默降级 |
| 5 | **检测优先（推理期降级）** | 检测出错只影响当次检测，不设 `enabled=False`，不影响后续检测能力 |
| 6 | **top_logprobs 跨请求恒定 + topk_n 参数传入** | 保证检测语义稳定；消除实例态 topk 首次锁定 |
| 7 | **resolver 优先 + bytes 兜底 + token_id 守卫** | 统一 token 文本来源，修复 `token_id:` 泄漏 |
| 8 | **独立 CollectorRegistry** | 与 vLLM 默认 `/metrics` 隔离，互不干扰 |

---

## 7. 整合设计

> 本节描述上层特性与 CATMonitor 底座的有机整合方案。完整整合设计见 [feature/elastic-ep/EEP_combination_DESIGN.md](../feature/elastic-ep/EEP_combination_DESIGN.md) 与 [feature/straggler/straggler_combination_DESIGN.md](../feature/straggler/straggler_combination_DESIGN.md)。

### 7.1 整合总体架构

```
                            ┌─────────────────────────────────────────────────────┐
                            │                CATMonitor daemon (Go)                 │
                            │  ┌──────────────────────────────────────────────────┐│
                            │  │ Scheduler 采集循环                                 ││
                            │  │   ↓ collectAndStore: metrics.Filter → Write       ││
                            │  │ Storage 装饰链（按 opt-in 装配）：                ││
                            │  │   StragglerStorage(若启用) ─┐                      ││
                            │  │   FaultStorage(若启用)     ─┼→ CachingStorage      ││
                            │  │                              └→ JSONLStorage 落盘 ││
                            │  │                                                  ││
                            │  │ FaultDetector → FaultEvent → Dispatcher           ││
                            │  │ faultsub Webhook 推送（net/http 异步）            ││
                            │  │ faultsub REST :9101（订阅/快照/事件/通用 ingest）  ││
                            │  │ StragglerStorage → straggler_kpi_{date}.jsonl     ││
                            │  └──────────────────────────────────────────────────┘│
                            └──────────────┬───────────────────────────┬───────────┘
                                           │ ① HTTP Webhook             │ ② KPI JSONL 文件
                                           │   FaultEvent                │   straggler_kpi_{date}.jsonl
                                           ▼                            ▼
        ┌──────────────────────────────────────────────┐    ┌───────────────────────────────┐
        │ EEP 外部故障管理中心 (Python)                 │    │ straggler (Go, 独立 module)     │
        │  - catmonitor_fault_sub.py                   │    │   一次性 / --daemon 守护进程     │
        │     HTTP server 接收 webhook FaultEvent      │    │   读 KPI 文件 → 空间 peer 对比   │
        │  - ZMQ SUB :22867 引擎健康（保留不变）        │    │   +（daemon）周期 dyno 采集 →    │
        │     NPU→DP 映射 → 调 vLLM REST API           │    │   python analyse 转 .db → 检测 │
        │     下发容错指令（pause/scale_down/retry）    │    │   命中慢卡 → HTTP/结果文件消费   │
        └──────────────┬─────────────────────────────┘    └───────────────┬────────────────┘
                       │ HTTP POST /fault_tolerance/apply                  │
                       ▼                                                   │
        ┌────────────────────────────────────────────┐                    │
        │ vLLM 容错框架（三级哨兵 + 缩容助手）         │                    │
        │   pause / scale_down / retry                │                    │
        └────────────────────────────────────────────┘                    │
                                                                          │
        [vLLM 进程内] accuracy-monitoring ASGI 中间件（不依赖 CATMonitor）   │
        │  透明拦截 → 注入 logprobs → 抽取 → 恢复 → ILLDetector 检测         │
        │  /anomaly/metrics + 异常落盘(pkl) + WebUI 告警                     │
        └──────────────────────────────────────────────────────────────────┘
```

### 7.2 EEP 与 CATMonitor 整合

#### 7.2.1 整合目标

EEP 的 `scale_down_demo.py`（外部故障管理中心）有**两条故障检测路径**：

| 路径 | 实现 | 数据来源 | 整合定位 |
|------|------|---------|---------|
| ① DCMI 硬件轮询 | `monitor_machine_fault()`，每 3s 用 ctypes 直接调 `libdcmi.so` 查询 NPU health/errorcode | 直接读 NPU 硬件 | **替换为订阅 CATMonitor** |
| ② 引擎健康订阅 | `start_monitor_engine_status()`，ZMQ SUB 订阅 vLLM PUB（端口 22867） | vLLM/EEP 内部上报 | **保留不动**（属 EEP 自身边界） |

整合目标：把路径①从"Demo 自己轮询 DCMI"改为"订阅 CATMonitor 推送的故障事件"。

#### 7.2.2 CATMonitor 侧改造

1. **新增 `features/faultsub/` 模块**：照 `features/exporter` 的"Storage 插件 + HTTP 端点"模式，作为 `collector.Storage` 装饰器包装在 `CachingStorage` 外。详见 §3.7.4。
2. **DCMI 错误码采集增强**：返回完整错误码 hex 列表（如 `0x40f84e00`）。`npu/error_code` 指标 Value 仍为计数（向后兼容 Prometheus），`Labels["error_codes"]` 存逗号分隔的完整 hex 列表。
3. **卡掉线故障态显式识别**：新增 `npu/card_drop` 指标（1=掉线, 0=正常），显式识别 `DeviceNotReady(-8012)`。
4. **配置扩展**：`catmonitor.yaml` 新增 `faultsub:` 段（`enabled`/`rest_addr`/`webhook_timeout`/`webhook_retry`/`event_buffer`/`defaults`/`rules`）。
5. **daemon 集成**：`runDaemon()` 装配 `FaultStorage` + REST API（受 `cfg.FaultSub.Enabled` 门控）。

Storage 链路：`Scheduler → FaultStorage(若启用) → CachingStorage → JSONLStorage`。未启用时与现状完全一致。

#### 7.2.3 EEP 侧改造

| 路径 | 改造 |
|------|------|
| ① DCMI 硬件轮询（`monitor_machine_fault`） | **删除**，替换为新订阅器 `catmonitor_fault_sub.py`（HTTP server 接收 webhook） |
| ② 引擎健康 ZMQ SUB（`start_monitor_engine_status`） | **保留不变**（属 EEP 内部边界） |
| 容错指令下发（`pause`/`scale`） | 保留，由新订阅器在收到故障事件后调用 |
| NPU→DP 映射（`dp_to_npu`/`npu_to_dp`） | 保留，留在 EEP 侧（CATMonitor 不感知 vLLM 部署拓扑） |

**新增订阅器 `catmonitor_fault_sub.py`** 工作流：
1. 启动 HTTP server（Python 标准库 `http.server.ThreadingHTTPServer`，默认端口 9102）。
2. 启动注册：调 CATMonitor `POST /faultsub/subscriptions`，注册订阅。
3. 接收 Webhook：CATMonitor 在故障时 `POST /fault_event`，body 为 JSON `FaultEvent`。
4. 故障→DP 映射：用 `npu_to_dp[event.NPUID]` 得到 `exclude_dp_ranks`。
5. 下发容错指令：调 vLLM `/fault_tolerance/apply`。
6. 恢复事件处理：`event.Recovered==true`（如 roce_link_down 恢复）触发 `retry`（网络闪断重推恢复）。
7. 优雅退出：进程退出前 `DELETE /faultsub/subscriptions/{id}` 注销订阅。

**故障类型 → EEP 恢复动作映射**：

| FaultEvent.type | EEP 动作 | 说明 |
|----------------|---------|------|
| `card_drop` / `npu_health`(Critical) / `hbm_uce` / `ddr_uce` | pause → scale_down（exclude_dp_ranks） | 不可恢复硬件故障，缩容 |
| `npu_error_code`（非卡掉线） | pause → 查 status → scale_down 或 retry | 视错误码严重程度 |
| `roce_link_down`（recovered=true） | retry | 网络闪断重推恢复 |
| `roce_link_down`（持续） | pause → 等待/人工 | 链路未恢复不自动缩容 |

### 7.3 Straggler 与 CATMonitor 整合

#### 7.3.1 整合目标（v0.2.3）

| # | 决策 | 选择 |
|---|------|------|
| 1 | 接入范围 | **仅第一道（KPI）** 接入 CATMonitor；第二道（Profiler）由守护进程的 dyno 采集 .db（应用级，超底座定位） |
| 2 | KPI 数据获取 | CATMonitor **新增 opt-in 的 straggler 专用 KPI 文件**（启用才输出，不启用零影响）；straggler 读该文件做空间 peer 对比 |
| 3 | 运行模式 | straggler 支持**一次性手动运行 + 常驻守护进程 `--daemon`**（周期自动采集+检测，HTTP 查询/控制） |
| 4 | 模块结构 | straggler 作为 **`feature/straggler` 独立 Go module**（自带 go.mod，重构 import），外部消费 CATMonitor 数据，与 EEP 结构一致 |
| 5 | 结果消费 | **守护进程 HTTP/结果文件消费**：v0.2.3 起不再向 faultsub 回注 `straggler_detected` 事件，命中慢卡由 daemon HTTP 接口（`/straggler/results/*`）或 `straggler_output.json`/`daemon_results/` 消费 |

整合目标：
1. **CATMonitor 新增 straggler KPI 输出**：opt-in 特性模块 `features/stragglerout`，按 straggler 需要的"每时刻含全部卡"格式输出 KPI 时序文件，默认关闭、零回归。详见 §3.7.5。
2. **straggler 改造为独立 module**：重构 import 路径；JSON reader 读 CATMonitor 输出文件；保留一次性模式，新增守护进程模式。
3. **补齐指标缺口**：CATMonitor 补充 `roce_new_pkt_rty` 计数器（straggler 需要、CATMonitor 原暂无）。

#### 7.3.2 CATMonitor 侧改造

1. **新增 `features/stragglerout/` 模块**：照 `features/faultsub`/`exporter` 的"Storage 插件"模式，作为 `collector.Storage` 包装在最外层，仅处理 NPU 批次。详见 §3.7.5。
2. **补齐指标缺口**：`internal/source/hccn_tool` 的 statistics 解析新增 `roce_new_pkt_rty`，并在 `metrics.yaml` 登记（Medium）。
3. **配置扩展**：`catmonitor.yaml` 新增 `straggler_output:` 段（`enabled`/`data_dir`/`retention`/`flush_interval`/`metrics`）。
4. **daemon 集成**：`runDaemon()` 装配 `StragglerStorage`（受 `cfg.StragglerOutput.Enabled` 门控）。

Storage 链：`Scheduler → StragglerStorage(若启用) → FaultStorage(若启用) → CachingStorage → JSONLStorage`。三者皆 opt-in、互不影响。

#### 7.3.3 straggler 侧改造（v0.2.3）

1. **独立 Go module + import 重构**：`feature/straggler/go.mod`（`module github.com/Computing-Availability-Tools/CATHelper/feature/straggler`，依赖 `modernc.org/sqlite`）。straggler **不 import CATMonitor 包**（外部消费其文件/REST），与 EEP 一致。
2. **JSON reader**（`resource/json_reader.go`）：读 `straggler_kpi_{date}.jsonl`（支持平铺与多节点子目录布局），复用后续聚合/检测管线。
3. **resource 模块重构**：移除时间维度/基线/检测窗口/根因定界（删除 `emit.go`/`fusion.go`/`rootcause.go`/`time_detector.go`/`baseline.go`），异常完全由空间 peer 对比判定；KPI 与 Profiler 共享 `clustering` 包 kmeans 比例算法。
4. **守护进程模式**（`daemon/` 包）：周期 dyno 触发采集 → python analyse 转 .db → 解析 → KPI+Profiler 检测 → 落盘 `daemon_results/<start>/` + HTTP 查询/控制。
5. **移除 faultsub 回注**：移除 `--faultsub-url` 参数与向 faultsub POST `straggler_detected` 事件的逻辑；命中慢卡改由 daemon HTTP 接口或结果文件消费。
6. **build.sh 一键构建**：aarch64 装 dyno/dynolog + Python wheel + Go 工具链 + go build；引入 `3rdparty/msmonitor` 子模块。

### 7.4 accuracy-monitoring 整合定位

accuracy-monitoring 作为 **vLLM 进程内 ASGI 中间件**运行，**不依赖 CATMonitor 底座**：

- **数据来源**：直接拦截 vLLM 的推理请求/响应，从模型输出的 logprobs/token_id 序列检测，不经 CATMonitor 采集管道。
- **与底座的关系**：可独立部署；其 Prometheus 端点 `/anomaly/metrics` 与 CATMonitor 的 `/metrics` 互不干扰（独立 `CollectorRegistry`、不同路径）。
- **与 EEP/straggler 的关系**：三者面向不同维度——EEP 处理硬件故障容错，straggler 处理性能劣化检测，accuracy-monitoring 处理输出精度异常检测；可并行部署于同一推理集群，互不耦合。
- **WebUI 独立**：可独立部署聚合多 vLLM 实例的精度异常，支持阈值告警与多渠道通知（钉钉/飞书/企业微信/邮箱）。

---

## 8. 端到端工作流

### 8.1 部署启动顺序（跨机示例：CATMonitor=10.0.0.10，EEP=10.0.0.5）

1. **构建并启动 CATMonitor daemon**（`catmonitor.yaml` 中 `faultsub.enabled: true`、`straggler_output.enabled: true` 按需）：
   ```bash
   cd CATHelper/CATMonitor && make all   # daemon + web + dfee 三二进制；NPU 环境加 -tags dcmi
   # 编辑 catmonitor.yaml，按需开启 faultsub / straggler_output / snapshot / features
   catmonitor daemon
   # 日志：exporter listening :9100；faultsub REST :9101；snapshot 生产
   ```
2. **部署带容错能力的 vLLM 服务**（详见 [feature/elastic-ep/README.md](../feature/elastic-ep/README.md)）：
   ```bash
   bash feature/elastic-ep/examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh \
       --dp-size 4 --fault-port 22867 --port 8006
   ```
3. **启动 EEP 外部故障管理中心**（订阅 CATMonitor + 引擎健康，跨机地址）：
   ```bash
   python feature/elastic-ep/examples/fault_tolerance_scale/scale_down_demo.py \
       --npu-ids 0,1,2,3 \
       --catmonitor-host 10.0.0.10 --catmonitor-rest-port 9101 \
       --callback-host 0.0.0.0 --callback-port 9102 \
       --advertise-url http://10.0.0.5:9102/fault_event \
       --port 8006 --recovery-timeout 120
   ```
4. **（可选）启动 straggler**：
   ```bash
   cd feature/straggler && bash build.sh   # aarch64 首次构建（daemon 模式必需）
   # 一次性模式（任意平台可编译）
   ./slowNodeDetection --kpi-jsonl-dir=/var/lib/catmonitor/straggler
   # 或守护进程模式（周期自动采集+检测，HTTP :8080）
   ./slowNodeDetection --daemon --profiler-dir=/data/profiler \
       --kpi-dir=/var/lib/catmonitor/straggler --interval=600
   ```
5. **（可选）部署 accuracy-monitoring**（vLLM 进程内中间件）：
   ```bash
   cd feature/accuracy-monitoring && pip install -e .
   vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware
   # 查看精度异常指标：curl http://localhost:8000/anomaly/metrics
   ```
6. **（可选）启动只读消费者**：
   ```bash
   catmonitor-web -addr :9527 -snapshot-dir /var/lib/catmonitor/snapshot
   catmonitor-dfee -addr :9528 -snapshot-dir /var/lib/catmonitor/snapshot
   ```

### 8.2 NPU 卡掉线故障全链路（EEP）

1. NPU 3 掉线 → DCMI `dcmi_get_device_health` 返回 -8012。
2. CATMonitor NPU 采集器（3s 周期）→ `npu/card_drop=1`、`error_code` 含 `0x40f84e00`。
3. `FaultStorage.Write` → `FaultDetector` 命中 `card_drop` 规则 → 生成 `FaultEvent{type:card_drop, npu_id:"3", severity:critical}`。
4. `Dispatcher` → Webhook `POST http://10.0.0.5:9102/fault_event`（去抖后）→ EEP 收到回 200。
5. EEP：`npu_to_dp["3"]=3` → `exclude_dp_ranks=[3]` → `pause(timeout, [3])` → `scale_down(timeout, [3])`。
6. vLLM 缩容，移除 DP rank 3，剩余健康 NPU 恢复推理。
7. （可选）故障恢复后 CATMonitor 推 `Recovered=true` 事件，EEP 据此可日志记录或 retry。

### 8.3 慢节点检测全链路（straggler）

**一次性模式**：
1. CATMonitor daemon 启用 `straggler_output` 后，每 3s 采集 NPU KPI → `StragglerStorage` 抽取 11 项 KPI 按时刻×按卡聚合 → 追加写当日 `straggler_kpi_{date}.jsonl`。
2. straggler 一次性运行，读 KPI 目录 → 10 秒聚合 → 取最后一个聚合点 → 空间 peer 对比（kmeans 比例，双方向各检一次）。
3. 命中慢卡：生成 `straggler_output.json` + stdout 报告（按指标分组列出异常卡及空间 score）。

**守护进程模式**：
1. 训练进程以 `MSMONITOR_USE_DAEMON=1` 启动；straggler daemon `--daemon` 启动，等待一个 `--interval` 后开始循环。
2. 每周期：daemon 拉起 dynolog → dyno 触发采集（命中 vllm 进程 + commandStatus=effective）→ 等待 `--collect-wait` → 对整个 `--profiler-dir` 根目录 python analyse 转 .db → dataparse 解析 → KPI 检测（读 `--kpi-dir`）+ Profiler 检测。
3. 合并 JSON + `daemon_meta.json` 落盘 `daemon_results/<start>/` → 周期结束删除整个 `--profiler-dir`。
4. 运维经 HTTP 查询：`GET /status`、`GET /straggler/results/latest`、`GET /straggler/report/latest`；控制：`POST /daemon/{pause,start,trigger,interval}`。

### 8.4 推理精度异常检测全链路（accuracy-monitoring）

1. vLLM 以 `--middleware anomaly_middleware.AnomalyMiddleware` 启动，中间件启动期 eager 加载 tokenizer/ILLDetector/进程池（fail-fast）。
2. 客户端 `POST /v1/chat/completions` → 中间件按 `monitor_rate` 抽中 → 读 body → 强制注入 `logprobs`/`top_logprobs=20`/`return_tokens_as_token_ids=true` → 修正请求 CL → 重放 receive → 装 `ResponseInterceptor` → 委托下游。
3. 下游响应：流式经 `SSEStreamProcessor` 增量 strip+转发同时累积检测数据；非流式缓冲后 extract+strip+重序列化发送。注入 `x-anomaly-request-id` 响应头。
4. 响应发送完毕后 fire-and-forget 调度检测：分配 SharedMemory 写入 `(logprobs, token_ids)` numpy 数组 → `ProcessPoolExecutor` worker 零拷贝读取 → `ILLDetector.detector(logprobs, token_ids, topk_n)` → 返回 `[[is_ill, ill_type]]`。
5. 检出异常（ill_type ∈ {1,2,3,4}）→ `vllm_anomaly_detected_total{ill_type,model}`++ +（若开启 `VLLM_ANOMALY_SAVE_PATH`）异常现场 pickle 落盘 + WebUI 告警（钉钉/飞书/企业微信/邮箱）。
6. 客户端完全无感知，收到正常推理响应（logprobs 按其原始请求参数恢复，token_id 还原为文本）。

---

## 9. 兼容性与风险

| 项 | 说明 |
|----|------|
| opt-in 默认关闭 | CATMonitor 的 `faultsub.enabled`、`straggler_output.enabled` 默认 `false`，不启用时 daemon 行为与现状完全一致，零回归风险；accuracy-monitoring 经 `VLLM_ANOMALY_ENABLED` 开关，关闭=纯透传 |
| 极简依赖 | CATMonitor 推送/REST 均用 `net/http`，不引入新 Go 依赖，保持"仅 yaml.v3"；straggler 仅 `modernc.org/sqlite`（纯 Go，无 CGo）；accuracy-monitoring 用 `numpy`/`prometheus_client`/`pyyaml`/`httpx`/`colorlog` |
| 采集开销 | `FaultDetector` 只处理 npu（及配置的）component 批次，纯内存判定；Webhook 仅在故障时推送；`StragglerStorage` 内存缓冲 + 60s flush，I/O 可控 |
| 构建标签 | CATMonitor 订阅机制无 CGo 依赖，默认构建即可用；DCMI 错误码/卡掉线增强受 `dcmi` 标签，非 NPU 环境降级；straggler Go 编译不依赖 dyno/dynolog（无 embed），daemon 模式运行时才要求 aarch64 主机装好二者 |
| 跨语言 | HTTP Webhook(JSON)，Go/Python 间用 JSON 文本契约，无二进制编码差异 |
| 跨机 | 回调 URL 由 EEP 注册时声明（`--advertise-url`），CATMonitor POST 到该地址；需保证 CATMonitor→EEP 网络可达 |
| 端口冲突 | 9100(exporter)/9101(faultsub)/9102(EEP webhook)/9527(web)/9528(dfee)/8006(vLLM)/22867(ZMQ)/8080(straggler daemon)/8000(vLLM, accuracy metrics 在同端口 `/anomaly/metrics` 路径)错开，均可配置 |
| 单点 | CATMonitor daemon 单进程；若 daemon 挂，EEP 订阅器收不到 webhook，EEP 仍可走引擎健康 ZMQ 路径②兜底；straggler daemon 单进程，重启后查询历史清空（只看本次会话） |
| 时序 | CATMonitor 采集周期 3s（NPU 默认），故障检测延迟 ≤3s + 去抖；EEP `engine_recovery_timeout_sec`(默认 120s) 远大于此，不影响容错窗口 |
| NPU→DP 映射 | 留 EEP 侧（CATMonitor 不感知 vLLM 部署拓扑）；订阅时按 `npu_ids` 过滤，订阅器本地维护映射 |
| 向后兼容 | `error_code` Value 仍为计数（Prometheus 不变），仅 labels 增 `error_codes`；`card_drop` 为新增指标 |
| straggler 文件量 | 3s 采样 × 8 卡 × 11 指标 ≈ 5.7MB/天/卡组；JSONL 追加写 + 60s flush，I/O 可控；15 天 ≈ 86MB，定时检测读取可接受 |
| straggler KPI 仅空间维度 | 无时间维度兜底；单节点在场卡 < 2 时该节点 score=0；`aicore_freq` 轻度降频（< 空间簇比例阈值 2.0×）不被标记 |
| straggler 不回注 faultsub | v0.2.3 起命中慢卡不再经 faultsub 闭环，需通过 daemon HTTP 接口或 `straggler_output.json`/`daemon_results/` 自行消费 |
| straggler daemon 真机依赖 | daemon 模式需 aarch64 + Ascend NPU + CANN + `torch_npu` + `mindstudio_monitor` wheel + dyno/dynolog；跨平台编译产物仅支持一次性模式 |
| msmonitor 子模块 | 克隆仓库后需 `git submodule update --init feature/straggler/3rdparty/msmonitor`（build.sh 引用），Go 编译本身不依赖 |
| accuracy-monitoring 启动 fail-fast | `detector.yaml`/env 越界/tokenizer 加载失败 → 终止 vLLM 启动；推理期错误才走降级（不阻塞推理） |
| accuracy-monitoring 进程池 | `BrokenProcessPool`（worker segfault/OOM）自动重建 + 该请求计 error，后续请求在新进程池正常检测 |
| accuracy-monitoring 透明性 | 不影响客户端响应状态、不阻塞响应返回、不泄漏内部参数；`/anomaly/metrics` 与 vLLM `/metrics` 独立隔离 |

---

## 10. 关键设计决策小结

| 决策 | 选择 | 理由 |
|------|------|------|
| 底座 + 特性分层 | CATMonitor 作底座，EEP/straggler/accuracy-monitoring 作上层特性 | 底座只做"采集 + 评估 + 输出/推送"，不感知上层业务；上层特性复用底座指标或独立面向推理高可用 |
| 模块独立 | CATMonitor、straggler 各自独立 `go.mod`，互不 import；EEP 为 Python + 补丁形态；accuracy-monitoring 为 Python ASGI 中间件包 | 耦合仅经文件/REST/Webhook/vLLM 插件契约，便于独立演进与跨机部署 |
| 接入点（CATMonitor） | `collector.Storage` 插件（`FaultStorage`/`StragglerStorage`） | 与 exporter 的 `CachingStorage` 同模式，零侵入采集管道 |
| 推送协议（CATMonitor↔EEP） | HTTP Webhook（CATMonitor POST → EEP server） | 用 `net/http` 零新依赖，跨机天然支持，异步不阻塞采集 |
| 消息编码 | JSON | 跨语言、易调试、无二进制依赖 |
| 订阅配置 | REST API（`POST /faultsub/subscriptions`） | 满足"告诉 CATMonitor 要什么故障/频率/回调地址"的需求，可动态增删 |
| 拉取兜底 | REST `/faultsub/snapshot` + `/faultsub/events` | 提供 poll 模式与故障回补能力 |
| 通用事件 ingest | REST `POST /faultsub/events` | 外部检测器可回注命中事件，复用 faultsub 分发能力（v0.2.3 起 straggler 不再使用） |
| 故障判定位置 | CATMonitor 侧（`FaultDetector`） | 集中判定，EEP 只消费事件；避免每个消费端重复实现 DCMI 判定 |
| NPU→DP 映射 | 留 EEP 侧 | CATMonitor 不感知 vLLM 部署拓扑，职责清晰 |
| 引擎健康路径 | 保留不变 | 属 EEP/vLLM 内部边界，非 CATMonitor 职责 |
| 错误码增强 | 返回完整 hex 列表 | EEP 靠具体错误码（0x40f84e00）判卡掉线 |
| 卡掉线识别 | 新增 `card_drop` 指标 + DeviceNotReady(-8012) 判定 | 显式化故障态，替代静默跳过 |
| KPI 数据获取 | CATMonitor opt-in 写专用 KPI JSONL 文件 | CATMonitor 做格式聚合，straggler 只读，契约清晰 |
| KPI 文件格式 | JSONL（每行一样本） | 追加 O(1)、日级轮转、流式读；与 CATMonitor 既有 JSONL 风格一致 |
| straggler 运行模式 | 一次性 + 守护进程 `--daemon` | 一次性按需手动；daemon 周期自动采集+检测+HTTP 控制，适合持续巡检 |
| straggler 接入范围 | 仅第一道(KPI) | CATMonitor 已采集 KPI；第二道(Profiler)属应用级，超底座定位（由 daemon 的 dyno 采集） |
| straggler KPI 算法 | 纯空间 peer 对比（kmeans 比例） | 无需历史基线/检测窗口，轻量常态化；唯一旋钮 `SpaceRatioThreshold` |
| straggler 不回注 faultsub | v0.2.3 移除 `--faultsub-url` 与回注 | 与守护进程结果上报重复；命中慢卡由 daemon HTTP/结果文件消费 |
| straggler 采集工具 | 系统安装 dyno/dynolog（非 embed） | 仓库不携带第三方制品；Go 产物与采集工具解耦，任何平台可出包 |
| KPI 优先 + Profiling 降级 | KPI 无侵入覆盖硬件层，Profiling 按需覆盖软件层 | 两者互补，避免无谓开销 |
| accuracy-monitoring 形态 | vLLM `--middleware` ASGI 中间件 | 直接拦截推理请求/响应，无侵入、零参照知识检测输出精度异常 |
| accuracy 检测调度 | fire-and-forget（响应发送完毕后） | 客户端永远不等待检测 |
| accuracy 检测执行 | 进程池 + 共享内存零拷贝 | numpy 数组零拷贝传 worker，GIL 不阻塞事件循环；进程池崩溃自动重建 |
| accuracy 启动期 | fail-fast（硬依赖失败即终止） | 避免静默降级运行；推理期错误才走降级 |
| accuracy 透明性 | 纯 ASGI 增量转发 + 独立指标 registry | 不影响客户端响应、不阻塞、不泄漏；与 vLLM `/metrics` 隔离 |
| 默认开关 | `faultsub.enabled`/`straggler_output.enabled` 默认 `false`；`VLLM_ANOMALY_ENABLED` 默认 `1`（可关） | 渐进采用，零回归 |
| 部署形态 | 支持跨机 | EEP 注册时声明可达回调 URL，CATMonitor 反向连接推送 |

---

## 11. 路线图

- **SGLang 支持**：EEP 后续版本计划支持 SGLang 框架。
- **真机验证**：NPU KPI 真实采集、Profiler `.db` 解析、端到端容错/检测/精度异常链路在昇腾 A3 真机复测。

---

*文档版本：v2.1 · 对应 CATHelper v0.2.4 · 整合对象：CATMonitor v0.3.3 + Elastic EP v0.1.0 + Straggler v0.2.3 + Accuracy-Monitoring v0.2.0 · 传输：HTTP Webhook + JSON + JSONL 文件 + vLLM ASGI 中间件 · 支持跨机*
