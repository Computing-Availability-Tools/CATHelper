# Elastic EP

> **Elastic EP** — 推理大EP卡级弹性容错

Elastic EP是CATHelper系列特性之一，实现推理大EP部署的卡级弹性容错，目前仅支持vLLM，后续计划也支持SGLang。 Elastic EP特性实现DP(data parallel)+EP(expert parallel)的部署模式下，卡故障之后，推理实例不退出，而是将故障卡所在的DP域隔离掉，重排专家后剩余DP继续提供推理服务，也支持网络闪断故障后请求重推恢复。

## 版本信息

> 详见 [Release_Notes.md](Release_Notes.md#v010)。

## 功能特性

> 详见 [Release_Notes.md §特性能力摘要](Release_Notes.md#特性能力摘要)。

## 技术栈

> 详见 [SPEC.md §1.2 技术栈](SPEC.md#12-技术栈)。

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
| `scale_down.py` | 模拟外部故障管理中心，内置两条检测路径：1）DCMI 轮询 NPU 硬件状态，检测卡掉线、HBM UCE 等硬件故障；2）ZMQ 订阅引擎健康状态，接收引擎异常上报（包括 kill 进程等软件故障），检测到故障后自动通过 REST API 发送缩容指令（可选） |

**serve_qwen.sh 参数：**

> 详见 [SPEC.md §5.1.3](SPEC.md#513-serve_qwensh-脚本参数)。

**scale_down.py 参数：**

> 详见 [SPEC.md §5.1.4](SPEC.md#514-scale_downpy-脚本参数)。

> **注意：** 运行前需根据实际环境修改脚本中的模型权重路径（`LOCAL_MODEL_PATH`、`MODEL_NAME` 等参数），或通过命令行参数覆盖。

---

#### 场景一：启动模拟的外部故障管理中心（自动响应故障）

模拟的外部故障管理中心通过 DCMI 轮询 NPU 健康状态，检测到故障后自动发送缩容指令（暂停由引擎异常自动触发），全程无需人工干预。

**步骤 1：启动支持容错的 vLLM 服务**

```bash
bash examples/fault_tolerance_scale/serve_qwen.sh \
    --dp-size 4 --redundant-experts 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

**步骤 2：启动外部故障管理中心demo(可选)**
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

模拟的外部故障管理中心检测到故障后自动执行：
1. 通过查询容错状态确认暂停完成
2. 发送缩容指令，移除故障 DP rank
3. 服务在剩余健康 NPU 上恢复，推理继续

---

#### 场景二：不启动模拟的外部故障管理中心（手动响应故障）


**步骤 1：启动支持容错的 vLLM 服务**

```bash
bash examples/fault_tolerance_scale/serve_qwen.sh \
    --dp-size 4 --redundant-experts 48 --fault-port 22867 --recovery-timeout 120 --port 8006
```

**步骤 2：发送推理请求**

服务就绪后，正常发送推理请求。


**步骤 3：注入故障**

模拟 NPU 故障，例如 kill 掉某个 Worker 进程。

**步骤 4：手动查询容错状态**

```bash
curl http://localhost:8006/fault_tolerance/status
```

返回结果中健康 DP rank 状态应为 `paused`，确认暂停成功。

**步骤 5：手动发送恢复指令**

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

## 特性兼容与限制

> 详见 [Release_Notes.md §兼容性与限制](Release_Notes.md#兼容性与限制)。

## 已测试模型

本特性已在以下模型上完成验证：

| 模型 | 量化 |
|------|------|
| DeepSeek-V3 | W8A8 |
| Qwen3-235B-A22B | W8A8 |
| GLM-5.1 | W8A8 |

## 已知问题

> 详见 [Release_Notes.md §已知问题](Release_Notes.md#已知问题)。

## 文档

| 文档 | 说明 |
|------|------|
| [SPEC.md](SPEC.md) | 技术规格与需求 |
| [DESIGN.md](DESIGN.md) | 架构与模块设计 |
| [Release_Notes.md](Release_Notes.md) | 版本发布记录 |
| [test_report.md](test_report.md) | 测试报告 |

### 项目结构

> 详见 [DESIGN.md §2.1 目录结构](DESIGN.md#21-目录结构)。
