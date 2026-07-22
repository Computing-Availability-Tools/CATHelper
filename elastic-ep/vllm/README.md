# vLLM Elastic EP

> **vLLM Elastic EP** — vLLM 弹性容错方案

vLLM Elastic EP 使 vLLM 能够在DP(data parallel)+EP(expert parallel)的部署模式下，出现故障后，推理进程进入暂停状态不退出，通过重试恢复服务，或者通过将故障点所在的DP组缩容掉的方式，将故障隔离出去，提供在线弹性能力。

## 版本信息

| 项目 | 说明 |
|------|------|
| 版本号 | v0.1.0 |
| 发布时间 | 2026-07-16 |
| 发布人 | sunnytao-blue |
| 平台支持 | Linux (ARM, Ascend NPU) |
| 许可证 | Apache License 2.0 |

## 功能特性

| 特性 | 说明 |
|------|------|
| **容错框架** | 三级哨兵架构（ClientSentinel / EngineCoreSentinel / NPUWorkerSentinel），支持通过 REST API 与外部的实例故障管理中心协同 |
| **故障上报** | 提供主动（外部实例故障管理中心通过 REST API）和被动（vLLM内部通过 ZMQ ）2种方式上报故障到Client层 |
| **优雅容错** | 故障发生时暂停实例，通过执行重试、缩容恢复指令实现快速自愈 |

## 技术栈

> 完整技术栈与依赖详见 [SPEC.md §1.2 技术栈](SPEC.md#12-技术栈) 和 [SPEC.md §6.1 依赖要求](SPEC.md#61-依赖要求)。

## 快速上手

### 前置条件

- 当前版本仅支持华为昇腾A3服务器

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
# 修复Bug：triton-ascend 3.2.1 仅适用于 vllm-ascend v0.20.0+，当前使用v0.18.0版本，triton-ascend需要降级到 3.2.0，否则安装会报错。
# https://github.com/vllm-project/vllm-ascend/issues/9794
sed -i 's/triton-ascend==3.2.1/triton-ascend==3.2.0/' pyproject.toml
pip install -e .
```

### 使用

项目提供拉起带容错功能的vLLM服务参考脚本位于 `examples/fault_tolerance_scale/` 目录下：

| 脚本 | 说明 |
|------|------|
| `serve_qwen.sh` | 启动带容错功能的 vLLM 服务 |
| `scale_down.py` | NPU 硬件故障监控和处理程序（可选：用户自行选择故障恢复策略时无需运行） |

> **注意：** 运行前需根据实际环境修改脚本中的模型权重路径（`LOCAL_MODEL_PATH`、`MODEL_NAME` 等参数），或通过命令行参数覆盖。

#### 启动 vLLM 服务

```bash
bash examples/fault_tolerance_scale/serve_qwen.sh \
    --dp 4 --re 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

#### 启动监控（可选）
监控程序是可选的。不启动时，框架仍会拦截引擎异常、自动暂停，并等待通过 REST API 手动发送 `retry`(重试) 或 `scale_down`(缩容)：

```bash
python examples/fault_tolerance_scale/scale_down.py \
    --npu-ids 0,1,2,3 --interval-time 3 \
    --external-fault-notify-port 22867 --port 8006
```


**查询当前容错状态：**

```bash
curl http://localhost:8006/fault_tolerance/status
```

**重试（重启所有 DP rank）：**

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"retry","params":{"timeout":30}}'
```

**缩容（排除指定 DP rank）：**

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"scale_down","params":{"timeout":30,"exclude_dp_ranks":[2]}}'
```

## 特性兼容情况

| 特性 | 状态 | 说明 |
|------|------|------|
| 动态 EPLB | 已兼容 | 故障后通过 EPLB 框架重新平衡专家放置 |
| 量化模型（W8A8） | 已兼容 | ModelSlim 格式 W8A8 量化模型已完成适配 |
| 量化模型（W4A8） | 暂未兼容 | W4A8 量化尚未适配 |
| MTP（多 Token 预测） | 已兼容 | 已完成适配，在 GLM5.1 上完成测试 |
| `--enforce-eager` 模式 | 已兼容 | 禁用图捕获 |
| PIECEWISE ACL Graph 模式 | 已兼容 | 大模型分块图捕获 |
| FULL Graph 模式 | 暂未兼容 | 大模型整图捕获 |

## 已测试模型

本特性已在以下模型上完成验证：

| 模型 | 量化 |
|------|------|
| DeepSeek-V3 | W8A8 |
| Qwen3-235B-A22B | W8A8 |
| GLM-5.1 | W8A8 |

其他类型的模型可能存在兼容性问题。

## 已知问题（v0.1.0）

第二次缩容存在一些偶现问题

## 限制

> 详见 [SPEC.md §6.2 限制](SPEC.md#62-限制)。

## 文档

| 文档 | 说明 |
|------|------|
| [SPEC.md](SPEC.md) | 技术规格与需求 |
| [DESIGN.md](DESIGN.md) | 架构与系统设计 |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | 版本发布记录 |
| [TEST_REPORT.md](TEST_REPORT.md) | 系统测试报告 |

### 项目结构

> 详见 [DESIGN.md §2.1 目录结构](DESIGN.md#21-目录结构)。
