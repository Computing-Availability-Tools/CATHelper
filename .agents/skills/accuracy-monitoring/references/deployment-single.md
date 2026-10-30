# 单机部署全集（C1 / W1）

检测组件以 vLLM `--middleware` 插件形式部署在推理服务进程内，随推理服务一同拉起。

## 前置条件

1. 目标节点 vLLM 版本支持 `--middleware`——**判据**：
   `vllm serve --help 2>&1 | grep -- '--middleware'`；**未命中时必须补查
   `vllm serve --help=all 2>&1 | grep -- '--middleware'`**（部分构建默认 help 不展示该参数，
   如 vllm-ascend v0.18.0）。任一命中=支持；两者皆未命中=不支持（把 help 原文贴给用户，
   结论收尾）。**禁止**翻查 vllm 源码、CLI 注册入口、参数名版本考证来解释原因；
2. **目标环境内已安装 anomaly_middleware**——由**用户**在目标环境内安装（Agent 检测与复验，
   不代装），任选其一：
   - 源码目录（推荐）：`cd /path/to/accuracy-monitoring/ && pip install -e .`
     （容器先 `docker exec -it <容器名> bash` 进入再执行）
   - wheel 离线：管理节点 `pip wheel .` 打包 → 传入目标环境 → `pip install <wheel 路径>`
   - 容器长期方案：Dockerfile 加 `RUN pip install /path/to/accuracy-monitoring` 后重建镜像
   （依赖 `prometheus_client`、`pyyaml`、`numpy`、`httpx`、`colorlog` 随装）
   缺失时 deploy_monitor.sh 拒绝拉起（exit 1）并输出上述指引；用户装好后重跑自动复检；
3. Agent 与服务不同机时，经 ansible 投递执行（见 remote-execution.md）。

## 容器目标（vllm 在 docker 内）

**检查**：`probe_env.sh --docker <容器名>`——python/包/vllm/进程检查经 `docker exec` 在容器内
执行（venv 场景加 `--vllm-bin <绝对路径>`）；硬件/卡数/端口映射为宿主机视角。
middleware 支持判据不变：容器内 `vllm serve --help` 输出 grep `--middleware`。
**禁止用 `docker inspect` 的 Mounts/镜像信息推断插件存在性**——装进镜像的插件没有挂载记录。

**middleware 进入容器（三选一，用户定）**：

| 方式 | 动作 | 持久性 |
|---|---|---|
| 进镜像（长期生产） | Dockerfile 加 `RUN pip install ...`，用户重建容器 | ✅ 持久 |
| 源码挂载 | 用户挂载源码卷 → `--pkg-path <容器内路径>` exec 安装 | ⚠️ 容器重建失效 |
| wheel 拷入 | 宿主机 `pip wheel .` → `docker cp` → `--pkg-path` exec 安装 | ⚠️ 容器重建失效 |

**拉起**：`deploy_monitor.sh --docker <容器名> ...`（`docker exec -d` 容器内后台化；
exec 安装随容器重建丢失需重装）。**验证**：HTTP 从宿主机直连容器发布端口即可（verify 脚本
`--port <映射端口>`），无需 exec。k8s 超出自动化范围：出修改建议、用户自行发布。

## 部署命令

```shell
# 方式一：直接命令（env 按需设置）
export VLLM_ANOMALY_SAVE_PATH=/data/anomaly/
vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware

# 方式二：经投递脚本（推荐，Agent 编排；后台化拉起）
sh <SKILL_DIR>/scripts/deploy_monitor.sh --model <模型> --save-path /data/anomaly/ \
    [--port P] [--tp N] [--monitor-rate R] [--launch-script <现有启动脚本>]
```

已有 systemd/docker 拉起方式时：`--launch-script` 传入现有启动脚本（脚本会先注入 env 再执行脚本）；
docker 部署建议将 env 通过 `-e` 注入容器、检测组件安装进镜像。

> **NPU 卡号语义（真实事故沉淀）**：多实例指定空闲卡用 `--env ASCEND_RT_VISIBLE_DEVICES=N`，
> 但该 env 取【逻辑卡号】（torch_npu 枚举序 0..N-1），**不是** `npu-smi info` 显示的【物理卡号】。
> 容器按物理号挂载 davinci2/davinci5 时逻辑号是 0/1——填物理号会在 `aclInit` 报 107001
> "Invalid device ID"，引擎启动即崩。先跑 probe 取 `npu_logical_count`/`npu_davinci`/空闲卡映射；
> deploy_monitor.sh 已内置该校验（非法值 rc=3 拒绝，值域不可得时强告警）。

## 环境变量（全部可选，有默认值）

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `VLLM_ANOMALY_ENABLED` | `1` | 总开关，`0`/`false` → 纯透传不检测 |
| `VLLM_ANOMALY_MONITOR_RATE` | `1.0` | 请求被异常监控的概率，范围 0.0-1.0，`0` 不检测 |
| `VLLM_ANOMALY_TOP_LOGPROBS` | `20` | 注入的 top-logprobs 数量，范围 1-20，运行期不可变 |
| `VLLM_ANOMALY_METRICS_PATH` | `/anomaly/metrics` | 指标端点路径 |
| `VLLM_ANOMALY_DETECTOR_WORKERS` | `4` | 检测进程池 worker 数，范围 ≥1（建议 1-16） |
| `VLLM_ANOMALY_TOKENIZER_MODEL` | 未设 | 显式指定 tokenizer 加载源（模型路径或 HF repo id）；未设则自动发现 |
| `VLLM_ANOMALY_SAVE_PATH` | 未设 | pkl 异常现场落盘路径（见下节）；**强烈建议开启** |
| `VLLM_ANOMALY_CONFIG_PATH` | `/anomaly/config` | 动态配置端点路径 |

## VLLM_ANOMALY_SAVE_PATH 细则

- 值以 `.pkl` 结尾 → **文件模式**（路径即文件）；否则 → **文件夹模式**（文件名=模型名.pkl）；
- **必须绝对路径**；文件夹模式目录须预先存在、文件模式父目录须预先存在（投递脚本会 `mkdir -p`
  预创建），否则启动 fail-fast 报「保存的路径不存在」；
- 目标 pkl 已存在（文件模式）或目录内已有既有 *.pkl（文件夹模式）→ probe 输出
  `save_path_file_exists`/`dir_pkls`（**只报告不动作**）；处置（沿用追加/改名/删除/换路径）
  必须由用户决策，Agent 不得默认删除或静默沿用（见 SKILL.md W1 pkl 落盘决策点）；
- 不设置 → 不落盘：无异常现场 → 明细报告无法生成、无法回查编号；
- 记录字段与编号规则见 pkl-data-contract.md。

## tokenizer 自动发现顺序

未设 `VLLM_ANOMALY_TOKENIZER_MODEL` 时按序尝试（均失败 → 启动报错并提示设置该变量）：

1. 同进程 `sys.argv` 中的 `--tokenizer <路径>`；
2. `sys.argv` 中 `vllm serve` 的 `<model>` 位置参数；
3. HF 缓存扫描（`huggingface_hub.scan_cache_dir()`，以模型名为 hint 补全 repo id）。

## 动态采样率（monitor_rate）

```shell
curl http://<host>:<port>/anomaly/config                       # 查询
curl -X POST http://<host>:<port>/anomaly/config \
  -H "Content-Type: application/json" -d '{"monitor_rate": 0.2}'   # 调整（即时生效）
```

- 不持久化：重启回退 env 初始值；
- 调整前须用户确认；调整后回读 gauge `vllm_anomaly_monitor_rate` 作为证据。

## 启动期 fail-fast（硬依赖失败 → 终止启动，不降级运行）

- `configs/detector.yaml` 缺失（须在仓库根目录下拉起，或确认安装完整）；
- env 越界（monitor_rate ∉ [0,1]、top_logprobs ∉ [1,20]、detector_workers < 1）；
- tokenizer 加载失败（提示设置 `VLLM_ANOMALY_TOKENIZER_MODEL`）；
- ILLDetector 构造失败（numpy 缺失 / yaml 格式 / 阈值非法）；
- `VLLM_ANOMALY_SAVE_PATH` 目录/父目录不存在，或非绝对路径；
- `VLLM_ANOMALY_ENABLED=0` 例外：纯透传模式，不校验检测依赖，指标端点仍可达报零值。
