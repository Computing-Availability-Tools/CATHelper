# vLLM Scale-Down

[English](README.md)

面向华为昇腾 910C NPU 的 vLLM 弹性容错方案。

## 概述

vLLM Scale-Down 使 vLLM 能够**容忍 NPU 故障而无需重启**。当 NPU 卡掉线或变得不健康时，系统自动检测故障、暂停受影响的 DP rank、缩容并重新分配专家，在剩余健康 NPU 上恢复服务。

## 核心特性

| 特性 | 说明 |
|------|------|
| **故障上报** | 引擎崩溃通过 ZMQ 在秒级内捕获并上报 |
| **优雅缩容** | 暂停受影响 rank，重新分配专家，重载权重 |
| **外部 API 控制** | REST API 支持 pause、retry、scale_down 指令 |

## 最新版本

| 版本 | 发布时间 | 发布人 |
|------|----------|--------|
| v0.1.0 | 2026-07-16 | a798347923 |

完整发布记录见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

## 快速上手

### 前置条件

- 华为昇腾 910C
- Docker，可拉取 `quay.io/ascend/vllm-ascend:v0.18.0-a3`
- DCMI 库（可选，仅 NPU 监控程序需要）

### 安装

#### Step 1：拉取官方 Docker 镜像

```bash
docker pull quay.io/ascend/vllm-ascend:v0.18.0-a3
docker run -it --net=host --ipc=host --privileged=true \
    -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    quay.io/ascend/vllm-ascend:v0.18.0-a3 bash
```

#### Step 2：打补丁

```bash
cd /vllm-workspace/vllm
git fetch --all && git checkout v0.18.0 && git reset --hard bcf2be96
git apply /path/to/patches/vllm_scale_down.patch

cd /vllm-workspace/vllm-ascend
git fetch --all && git checkout v0.18.0 && git reset --hard 4a533861
git apply /path/to/patches/vllm_ascend_scale_down.patch
```

#### Step 3：安装

```bash
cd /vllm-workspace/vllm
VLLM_TARGET_DEVICE=empty pip install -e .

cd /vllm-workspace/vllm-ascend
git submodule update --init --recursive
# 修复：triton-ascend 3.2.1 仅适用于 v0.20+，降级到 3.2.0
sed -i 's/triton-ascend==3.2.1/triton-ascend==3.2.0/' pyproject.toml
pip install -e .
```

### 使用

参考脚本位于 `examples/Fault-Tolerance-scale/` 目录下：

| 脚本 | 说明 |
|------|------|
| `serve_qwen.sh` | 启动带容错功能的 vLLM 服务 |
| `scale_down.py` | NPU 硬件故障监控程序（可选） |

> **注意：** 运行前需根据实际环境修改脚本中的模型权重路径（`LOCAL_MODEL_PATH`、`MODEL_NAME` 等参数），或通过命令行参数覆盖。

#### 启动 vLLM 服务

```bash
bash examples/Fault-Tolerance-scale/serve_qwen.sh \
    --dp 4 --re 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

#### 启动监控（可选）

```bash
python examples/Fault-Tolerance-scale/scale_down.py \
    --npu-ids 0,1,2,3 --interval-time 3 \
    --external-fault-notify-port 22867 --port 8006
```

监控程序是可选的。不启动时，框架仍会拦截引擎异常、自动暂停，并等待通过 REST API 手动发送 `retry` 或 `scale_down`：

**查询当前容错状态：**

```bash
curl http://localhost:8006/fault_tolerance/status
```

**重试（重启所有 DP rank）：**

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"retry"}'
```

**缩容（排除指定 DP rank）：**

```bash
curl -X POST http://localhost:8006/fault_tolerance/apply \
    -H "Content-Type: application/json" \
    -d '{"instruction":"scale_down","params":{"timeout":30,"exclude_dp_ranks":[2]}}'
```

## 特性支持情况

| 特性 | 状态 | 说明 |
|------|------|------|
| 动态 EPLB | 已完全适配 | 故障后通过 EPLB 框架重新平衡专家放置 |
| 量化模型（W8A8） | 已支持 | Ascend 格式 W8A8 量化模型已完成适配 |
| MTP（多 Token 预测） | 已支持 | 已完成适配，在 GLM5 上完成测试 |
| PIECEWISE ACL Graph 模式 | 已支持 | 大模型分块图捕获 |

## 已知问题（v0.1.0）

在**第二次缩容**时，以下问题偶现：

1. **故障权重加载时长大幅增加** —— 第二次缩容后重新加载权重耗时远超第一次
2. **`stop device` 无法停下** —— device 暂停无法完成，阻塞缩容流程
3. **恢复后卡在 worker 的 `input_event` 同步中** —— worker 在恢复后等待 input_event 同步时卡住

## 已测试模型

本特性已在以下模型上完成验证：

- DeepSeek-V3（DSv3）
- Qwen3-235B-A22B
- GLM5

其他类型的模型可能存在兼容性问题。

## 限制

| 限制 | 说明 |
|------|------|
| 仅支持 Ascend 910C | 当前仅支持华为昇腾 910C NPU |
| 必须开启 EP | 需启用 Expert Parallel（`--enable-expert-parallel`）才能使用容错特性 |
| 仅支持 TP = 1 | 缩容仅支持 tensor parallel size 为 1 |
| 不支持扩容 | 缩容后无法添加 NPU |
| 不支持 Pipeline Parallel | PP 与缩容不兼容 |
| 冗余专家数量要求（缩容限制） | 健康卡上的冗余专家总数必须大于故障卡上的非冗余专家数量 |

## 文档

| 文档 | 说明 |
|------|------|
| [SPEC.md](SPEC.md) | 规格、配置与 REST API 参考 |
| [DESIGN.md](DESIGN.md) | 架构与系统设计 |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | 版本发布记录 |
| [TEST_REPORT.md](TEST_REPORT.md) | 系统测试报告 |

## 许可证

Apache License 2.0
