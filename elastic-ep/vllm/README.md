# Elastic EP

> **Elastic EP** — 推理大EP卡级弹性容错

Elastic EP是CATHelper系列特性之一，实现推理大EP部署的卡级弹性容错，目前仅支持vLLM，后续计划也支持SGLang。 Elastic EP特性实现DP(data parallel)+EP(expert parallel)的部署模式下，卡故障之后，推理实例不退出，而是将故障卡所在的DP域隔离掉，重排专家后剩余DP继续提供推理服务，也支持网络闪断故障后请求重推恢复。

## 版本信息

| 项目 | 说明 |
|------|------|
| 版本号 | v0.1.0 |
| 发布时间 | 2026-07-24 |
| 发布人 | sunnytao-blue |
| 框架支持 | vLLM + vLLM-Ascend |
| 许可证 | Apache License 2.0 |

## 功能特性

| 特性 | 说明 |
|------|------|
| **外部协同** | 通过vLLM内新增的容错框架，支持通过 REST API 与外部(如推理服务化框架)故障管理中心协同容错，REST API支持故障上报、弹性容错命令 |
| **故障检测** | 支持主动通告（ZMQ）和被动查询（外部故障管理中心REST API查询）2种方式，将容错框架检查到故障上报至外部，由外部决策容错方式 |
| **弹性容错** | 支持接收外部故障管理中心决策的弹性容错命令，当前版本支持请求重推、缩容恢复两种命令，分别对应网络瞬时故障、卡/节点故障 |

## 技术栈

| 项目 | 选型 |
|------|------|
| 开发语言 | Python 3.10+ |
| 基础框架 | vLLM v0.18.0 + vllm-ascend v0.18.0 |
| 配置方式 | vLLM CLI 启动参数 + JSON 配置文件 |
| 外部依赖 | 当前版本作为vLLM的patch，无新增依赖 |

## 快速上手

### 前置条件

- 华为昇腾A3服务器：当前版本仅支持A3

### 安装

#### Step 1：拉取官方 vLLM Ascend Docker 镜像

```bash
docker pull quay.io/ascend/vllm-ascend:v0.18.0-a3
docker run -it --net=host --ipc=host --privileged=true \
    -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    quay.io/ascend/vllm-ascend:v0.18.0-a3 bash
```

#### Step 2：打容错框架补丁

```bash
cd /vllm-workspace/vllm
git fetch --all && git checkout v0.18.0 && git reset --hard bcf2be96
git apply /path/to/patches/vllm_scale_down.patch

cd /vllm-workspace/vllm-ascend
git fetch --all && git checkout v0.18.0 && git reset --hard 4a533861
git apply /path/to/patches/vllm_ascend_scale_down.patch
```

#### Step 3：安装vllm和vllm-ascend

```bash
cd /vllm-workspace/vllm
VLLM_TARGET_DEVICE=empty pip install -e .

cd /vllm-workspace/vllm-ascend
git submodule update --init --recursive
# 已知issue：triton-ascend 3.2.1 仅适用于 vllm-ascend v0.20.0+，当前使用v0.18.0版本，triton-ascend需要降级到 3.2.0，否则安装会报错。
# 参考：https://github.com/vllm-project/vllm-ascend/issues/9794
sed -i 's/triton-ascend==3.2.1/triton-ascend==3.2.0/' pyproject.toml
pip install -e .
```

### 使用

`examples/fault_tolerance_scale/` 目录下提供一个演示demo,可以启动支持容错的vLLM服务、以及一个模拟的外部故障管理中心；

| 脚本 | 说明 |
|------|------|
| `serve_qwen.sh` | 启动支持弹性容错功能的 vLLM 服务，模型Qwen3-30B-A3-W8A8 |
| `scale_down.py` | 模拟外部故障管理中心，支持通过DCMI监控NPU故障，并通过REST API触发弹性容错（可选：用户也可以注入故障后，手动通过REST API触发容错） |

**serve_qwen.sh 参数：**

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `--dp` | `4` | 数据并行大小，启动的 DP rank 数量 |
| `--re` | `0` | 冗余专家数量，每个 rank 额外放置的专家数，用于缩容时重新分配 |
| `--fault-port` | `22867` | 外部故障通知端口，ClientSentinel 通过 ZMQ PUB 广播引擎健康状态 |
| `--recovery-timeout` | `120` | 引擎恢复超时（秒），故障暂停后等待重试/缩容指令的最长时间，超时则抛异常退出 |
| `--port` | `8006` | HTTP API 端口，提供推理服务和容错 REST API |
| `--host` | `0.0.0.0` | 监听地址 |
| `--model-name` | 见脚本 | 模型在 API 中的展示名称 |
| `--local-model` | 见脚本 | 本地模型权重路径 |
| `--gloo-timeout-seconds` | `30` | Gloo 通信组超时（秒），用于通信组重建 |

**scale_down.py 参数：**

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `--npu-ids` | `0-15` | 参与推理的 NPU 设备 ID 列表（逗号分隔） |
| `--interval-time` | `3` | NPU 健康状态轮询间隔（秒） |
| `--external-fault-notify-port` | `22867` | 订阅引擎健康状态的 ZMQ SUB 端口（需与 vLLM 的 `--fault-port` 一致） |
| `--port` | `8006` | vLLM API 端口，用于发送暂停/缩容指令 |
| `--host` | `localhost` | vLLM API 主机地址 |
| `--recovery-timeout` | `30` | 容错指令的超时等待时间（秒） |

> **注意：** 运行前需根据实际环境修改脚本中的模型权重路径（`LOCAL_MODEL_PATH`、`MODEL_NAME` 等参数），或通过命令行参数覆盖。

---

#### 场景一：启动监控脚本（自动故障响应）

监控脚本通过 DCMI 轮询 NPU 健康状态，检测到故障后自动下发暂停和缩容指令，全程无需人工干预。

**步骤 1：启动支持容错的 vLLM 服务**

```bash
bash examples/fault_tolerance_scale/serve_qwen.sh \
    --dp 4 --re 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

**步骤 2：启动外部故障光临中心demo(可选)**
不启动demo时，vLLM内的容错框架仍会拦截异常，并等待容错命令，用户也可以通过REST API手动发送'retry(重试)'或'scale_down(缩容)'
```bash
python examples/fault_tolerance_scale/scale_down.py \
    --npu-ids 0,1,2,3 --interval-time 3 \
    --external-fault-notify-port 22867 --port 8006
```

**步骤 3：发送推理请求**

服务就绪后，正常发送推理请求。

**步骤 4：注入故障**

模拟 NPU 故障，例如 kill 掉某个 Worker 进程。

**步骤 5：等待自动恢复**

监控脚本检测到故障后自动执行：
1. 通过 REST API 发送暂停指令
2. 通过查询容错状态确认暂停完成
3. 发送缩容指令，移除故障 DP rank
4. 服务在剩余健康 NPU 上恢复，推理继续

---

#### 场景二：不启动监控脚本（手动故障响应）

不启动监控脚本时，框架仍会拦截引擎异常并自动暂停，需通过 REST API 手动发送恢复指令。

**步骤 1：启动支持容错的 vLLM 服务**

```bash
bash examples/fault_tolerance_scale/serve_qwen.sh \
    --dp 4 --re 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

**步骤 2：发送推理请求**

服务就绪后，正常发送推理请求。


**步骤 3：注入故障**

模拟 NPU 故障，例如 kill 掉某个 Worker 进程。

**步骤 4：查询容错状态**

```bash
curl http://localhost:8006/fault_tolerance/status
```

返回结果中健康 DP rank 状态应为 `paused`，确认暂停成功。

**步骤 5：发送恢复指令**

根据故障类型选择重试或缩容：

```bash
# 重试（重启所有 DP rank）
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"retry","params":{"timeout":30}}'

# 缩容（排除指定 DP rank）
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"scale_down","params":{"timeout":30,"exclude_dp_ranks":[2]}}'
```

## 兼容性与限制

| 特性 | 状态 | 说明 |
|------|------|------|
| 动态 EPLB | 已兼容 | 支持故障后通过 EPLB 框架重新平衡专家放置 |
| 量化模型 | 部分兼容 | 仅兼容 W8A8（ModelSlim 格式），W4A8、W4A16 等暂不支持 |
| MTP（多 Token 预测） | 已兼容 | 已完成适配，在 GLM5.1 上完成测试 |
| Eager 模式 | 已兼容 | 逐算子执行，禁用图捕获 |
| PIECEWISE ACL Graph 模式 | 已兼容 | 支持大模型分块图捕获 |
| FULL Graph 模式 | 暂未兼容 | 不支持大模型整图捕获 |
| 平台支持 | 仅华为昇腾 A3 | 当前版本仅支持华为昇腾 A3 服务器 |
| Pipeline Parallel | 不支持 | 不支持流水线并行 |
| Expert Parallel | 必须开启 | 容错特性必须开启 Expert Parallel（`--enable-expert-parallel`） |
| 冗余专家数 | 有约束 | 健康卡上的冗余专家总数必须大于故障卡上的非冗余专家数量 |

## 已测试模型

本特性已在以下模型上完成验证：

| 模型 | 量化 |
|------|------|
| DeepSeek-V3 | W8A8 |
| Qwen3-235B-A22B | W8A8 |
| GLM-5.1 | W8A8 |

其他类型的模型可能存在兼容性问题。

## 已知问题（v0.1.0）

缩容后，再次缩容存在一些偶现的问题，会导致缩容不成功。

## 文档

| 文档 | 说明 |
|------|------|
| [SPEC.md](SPEC.md) | 技术规格与需求 |
| [DESIGN.md](DESIGN.md) | 架构与模块设计 |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | 版本发布记录 |
| [TEST_REPORT.md](TEST_REPORT.md) | 测试报告 |

### 项目结构

> 详见 [DESIGN.md §2.1 目录结构](DESIGN.md#21-目录结构)。
