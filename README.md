# CATHelper

> CATHelper是CAT技术架构的主体部分，服务于鲲鹏和昇腾服务器，提供全栈故障指标采集、分析和容错恢复能力，方便被集成，以及使能大型生产环境的高可用特性开发。

**Computing Availability Tools** 系列的 helper 软件，采用"底座 + 上层特性"的分层架构：底座提供全栈指标采集与健康度评估能力，上层承载面向推理高可用场景的容错恢复特性。

| 项目 | 说明 |
|------|------|
| 版本号 | v0.2.4 |
| 发布时间 | 2026-09-08 |
| 许可证 | Apache-2.0 |

## 组成

```
CATHelper/
├── CATMonitor/            # 底座：全栈指标采集、健康度评估、Prometheus 导出守护进程
│   ├── internal/          #   采集核心 + 7 部件采集器 + 14 来源层
│   ├── features/          #   健康度 / snapshot 统一生产 / Web 仪表盘 / 能效监控 / Prometheus 导出 / 故障订阅推送 / KPI 输出
│   └── configs/           #   catmonitor.yaml + metrics.yaml
└── feature/
    ├── elastic-ep/        # 上层特性：推理大EP卡级弹性容错（EEP）
    │   ├── patches/       #   vLLM + vLLM-Ascend 容错框架补丁
    │   └── examples/      #   容错服务启动脚本 + 外部故障管理中心（订阅 CATMonitor）
    ├── straggler/         # 上层特性：慢节点（慢卡）检测
    │   ├── main.go        #   统一入口（一次性 + --daemon 守护进程）
    │   ├── daemon/        #   守护进程：dyno/dynolog 采集 + 周期检测 + HTTP 查询/控制
    │   ├── resource/      #   第一道 KPI 资源检测（空间 peer 对比 + 共享 kmeans）
    │   ├── profiling/     #   第二道 Profiler 检测（读 Ascend .db）
    │   ├── clustering/    #   共享 kmeans 比例检测算法
    │   ├── build.sh       #   aarch64 一键构建（dyno/dynolog + Python wheel + go build）
    │   └── 3rdparty/msmonitor  # msmonitor 子模块（build.sh 引用）
    └── accuracy-monitoring/  # 上层特性：推理精度异常检测（vLLM ASGI 中间件）
        ├── anomaly_middleware/  # 中间件包：拦截注入 / 恢复抽取 / 检测调度 / Prometheus
        ├── webui/         #   多实例聚合监控 Web（阈值告警 + 通知 + 历史数据导入）
        └── configs/       #   detector.yaml + webui.yaml
```

### 底座 — [CATMonitor](CATMonitor/)

服务器运行指标采集、健康度评估与 Prometheus 导出守护进程。覆盖 CPU / 内存 / 硬盘 / GPU / NPU / 网卡 / 机箱共 7 个部件、204 个指标；输出 JSONL 落盘 + Prometheus `/metrics`；提供故障信息订阅/推送机制（`faultsub`）供上层特性获取故障事件。**可独立运行，也可作为 CATHelper 的一部分。** 构建用法见 [CATMonitor/README.md](CATMonitor/README.md)，使用手册见 [CATMonitor/docs/User_Manual.md](CATMonitor/docs/User_Manual.md)。

> CATMonitor 子目录为其主干快照，保持独立 Go module（`github.com/Computing-Availability-Tools/CATMonitor`），可在 `CATMonitor/` 内独立 `go build`/`make build`。

### 上层特性 — [EEP（推理卡级弹性容错）](feature/elastic-ep/)

推理大 EP 卡级弹性容错特性（Elastic EP）。实现 DP+EP 部署模式下卡故障后推理实例不退出，而是隔离故障卡所在 DP 域、重排专家后剩余 DP 继续提供推理服务，并支持网络闪断故障后请求重推恢复。当前仅支持 vLLM，后续计划支持 SGLang。详见 [feature/elastic-ep/README.md](feature/elastic-ep/README.md)。

EEP 的故障信息输入已与 CATMonitor 底座有机整合：通过 `faultsub` 订阅机制，CATMonitor 采集并判定 NPU 故障（卡掉线 / 健康状态 / 错误码 / HBM UCE / RoCE 链路等），经 HTTP Webhook 推送给 EEP 的外部故障管理中心，由其映射 NPU→DP rank 后下发容错指令。整合设计见 [EEP_combination_DESIGN.md](feature/elastic-ep/EEP_combination_DESIGN.md)。

### 上层特性 — [Straggler 慢节点检测](feature/straggler/)

慢节点（慢卡）检测特性。两道防线：第一道（KPI 资源指标检测）基于 NPU 资源指标做**空间 peer 对比**（取最后一个聚合点，同节点卡互比，共享 kmeans 比例检测，无历史基线/检测窗口）；第二道（Profiler 检测）读 Ascend PyTorch Profiler `.db`，均质化聚类检测慢计算/慢通信/慢CPU（按物理节点 hostUid 分组）/NPU Bubble。两道结果合并输出为一份 JSON。详见 [feature/straggler/README.md](feature/straggler/README.md)。

straggler 既支持一次性手动运行，也支持**常驻守护进程模式**（`--daemon`）：周期性自动完成「触发采集 → 转换 → 解析 → 检测」全链路，结果通过 HTTP 查询与运维控制（`GET /status`/`/straggler/results/*`/`/straggler/op_metric/*`、`POST /daemon/{start,pause,interval,trigger,stop}`，默认端口 `:8080`，`--profiler-iterations` 控制 dyno 采集步数），适合接入运维/调度系统持续巡检。第一道（KPI）的数据来源已与 CATMonitor 底座有机整合：CATMonitor 通过 opt-in 的 `stragglerout` 模块输出专用 KPI 时序文件（`straggler_kpi_{date}.jsonl`），straggler 读该文件检测；第二道（Profiler）保留独立。整合设计见 [straggler_combination_DESIGN.md](feature/straggler/straggler_combination_DESIGN.md)。

> v0.2.3 起 straggler 不再向 faultsub 回注 `straggler_detected` 事件（命中慢卡改由 daemon HTTP 接口或结果文件消费）；KPI 时间维度/历史基线/根因定界已移除。

### 上层特性 — [Accuracy-Monitoring 推理精度异常检测](feature/accuracy-monitoring/README.md)

基于 vLLM `--middleware` 插件部署的纯 ASGI 中间件（Python 包 `anomaly_middleware`），在**无侵入、零参照知识**条件下实时检测推理输出崩溃类故障：生僻字 / 乱码 / 重复 / NaN 四类。中间件透明拦截请求并强制注入 logprobs 采集参数（top_logprobs 跨请求恒定），响应后恢复客户端原始参数语义，后台 fire-and-forget 异常检测，经独立 Prometheus 端点（`/anomaly/metrics`）暴露结果；检出异常现场可落盘（含输出 token id 与思维链文本），监控概率运行时可经 `/anomaly/config` 动态调整。配套多实例聚合 WebUI（阈值告警 + Webhook/邮箱通知 + 历史数据导入）。**不依赖 CATMonitor 底座**，随 vLLM 进程内运行。详见 [feature/accuracy-monitoring/README.md](feature/accuracy-monitoring/README.md)。

## 路线图

- **SGLang 支持**：EEP 后续版本计划支持 SGLang 框架。
- **真机验证**：NPU KPI 真实采集、Profiler `.db` 解析、端到端容错/检测链路在昇腾 A3 真机复测。

## 文档

| 文档 | 说明 |
|------|------|
| [SPEC.md](SPEC.md) | 功能规格说明书（面向使用者的整体功能介绍） |
| [User_Manual.md](User_Manual.md) | 使用手册（构建、安装、配置、底座与特性用法） |
| [Release_Notes.md](Release_Notes.md) | 版本发布记录 |
| [CATMonitor/](CATMonitor/) | 底座子项目（README / SPEC / DESIGN / 使用手册 / 指标清单） |
| [feature/elastic-ep/](feature/elastic-ep/) | EEP 特性子项目（SPEC / DESIGN / Release_Notes / 测试报告） |
| [feature/straggler/](feature/straggler/) | Straggler 特性子项目（README / SPEC / DESIGN / 整合设计） |
| [feature/accuracy-monitoring/](feature/accuracy-monitoring/) | 推理精度异常检测子项目（README / spec / design / webui_README / 测试报告） |

## 快速上手

```bash
# 1. 构建并启动底座（采集 + Prometheus :9100 + 故障订阅 REST :9101）
cd CATMonitor && make all            # 一次性构建 daemon + web + dfee 三个二进制
# 编辑 configs/catmonitor.yaml，设 faultsub.enabled: true（按需）
./bin/catmonitor daemon

# 2. 部署带容错能力的 vLLM 服务（详见 feature/elastic-ep/README.md）
bash feature/elastic-ep/examples/fault_tolerance_scale/ft_vllm_serve_qwen.sh --dp-size 4 --fault-port 22867 --port 8006

# 3. 启动外部故障管理中心（订阅 CATMonitor 故障事件 + 引擎健康状态）
python feature/elastic-ep/examples/fault_tolerance_scale/scale_down_demo.py \
    --npu-ids 0,1,2,3 --catmonitor-host localhost --catmonitor-rest-port 9101 \
    --callback-port 9102 --advertise-url http://localhost:9102/fault_event --port 8006

# 4.（可选）慢节点检测：CATMonitor 启用 straggler_output 后，跑 straggler
cd feature/straggler && go build -o slowNodeDetection .
# 一次性模式（KPI 空间检测；或加 path=<profiler_dir> 联合 Profiler）
./slowNodeDetection --kpi-jsonl-dir=/var/lib/catmonitor/straggler
# 守护进程模式（aarch64 + Ascend，周期自动采集+检测，HTTP :8080）
bash build.sh   # 首次：装 dyno/dynolog + Python wheel + 编译
./slowNodeDetection --daemon --profiler-dir=/data/profiler \
    --kpi-dir=/var/lib/catmonitor/straggler --interval=600 --daemon-port=8080
```

> 完整使用说明见 [使用手册](User_Manual.md)，功能概览见 [SPEC.md](SPEC.md)。
