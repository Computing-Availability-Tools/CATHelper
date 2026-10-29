# PD 分离部署全集（C2 / W2）

PD 分离（Prefill/Decode Disaggregation）场景下，检测能力**上移至服务化代理（load_balance_proxy，
下称 proxy）**，作为全局唯一检测点；P/D 节点启动命令**保持纯净**（不附加 `--middleware`）。

## 与单机模式的核心差异

- 检测点唯一：全局一份采样配置、一个 metrics 端点、一份 pkl 落盘、一个 webui 数据源；
  多 D 节点不会产生多份结果；
- **不加载 tokenizer**：检测所需词表类别映射（tk2cat）与 token 文本表由离线预生成文件提供；
- 全局采样精确单点：请求级检测概率全局精确等于 `monitor_rate`；
  `POST /anomaly/config` 一次调用全局生效；
- 拦截范围与注入/恢复行为与单机一致（仅 `/v1/chat/completions` 与 `/v1/completions`）。

## 第一步：离线预生成词表文件

在任意有模型/Tokenizer 的机器执行（仅依赖 `transformers`，无需 GPU/NPU、不访问网络）：

```shell
python <仓库根>/tools/gen_token_category.py --model-path <模型目录> [--output-dir <目录>]
# 产物（同名成对）：
#   token2category/<model_name>_<vocab_size>.json   # 全词表类别映射（检测器 tk2cat）
#   token_text/<model_name>_<vocab_size>.json       # 全词表 token 文本表（响应恢复用）
```

产物需投递到 proxy 所在节点（`ansible fetch`/copy 均可）。

## 第二步：选择集成模式（三选一，互斥——叠加会双重采样/注入）

| 模式 | 方式 | proxy 脚本改动 | 适用 |
|---|---|---|---|
| A. handler 挂钩 | `ProxyAnomalyController` 挂入 proxy 请求处理函数（`before_forward`/`feed_stream`/`on_recompute`/`finish`） | 多处 | 深度定制/调试；需精细感知 recompute |
| B. 一行包裹 | proxy 入口处 `app = build_proxy_middleware(app)`（复用单机版 ASGI 中间件） | 1 行 | 版本升级后快速重挂 |
| C. 独立启动器（**默认推荐**） | `run_proxy_with_anomaly.py` 零修改运行任意版本 proxy 脚本 | 0 行 | 不改上游脚本，随版本直接替换 |

## 第三步：配置环境变量

| 环境变量 | 说明 |
|---|---|
| `VLLM_ANOMALY_TOKEN2CATEGORY` | **PD 模式启用检测必填**：tk2cat 映射文件路径（缺失/非法 → proxy 启动终止） |
| `VLLM_ANOMALY_TOKEN_TEXT` | 可选覆盖 token 文本表路径；默认取映射文件兄弟目录 `token_text/` 下同名文件 |
| `VLLM_ANOMALY_SAVE_PATH` 等 | 语义与单机一致（见 deployment-single.md） |
| `VLLM_ANOMALY_TOKENIZER_MODEL` | PD 模式**不使用**（不加载 tokenizer） |

## 第四步：拉起 proxy（在 proxy 所在节点执行，异机经 ansible）

```shell
# 模式 C：独立启动器（--anomaly-* 自动映射为 VLLM_ANOMALY_* 环境变量）
python <仓库根>/tools/run_proxy_with_anomaly.py <proxy脚本> [proxy原参数] \
    --anomaly-token2category /opt/tk/token2category/qwen3_151936.json \
    --anomaly-save-path /data/anomaly/
```

- proxy 原参数原样透传；未配置词表文件且未显式禁用 → 按未启用检测运行（行为与原版一致）；
- 启动器检测到 proxy 脚本已内置挂钩（`_init_anomaly`）→ 拒绝启动并提示，防止双重集成
  （此时改用模式 A/B）；
- proxy 为 CPU 服务，无加速卡校验需求；P/D 节点零改动。

## 第五步：验证

同单机验证清单（V1-V4），目标地址为 **proxy** 地址；另加一条：客户端请求响应头
`x-anomaly-request-id` 存在（proxy 注入后透传给客户端）。

## recompute 一致性说明

D 节点返回 `stop_reason=="recomputed"` 时 proxy 会重构请求重发（续写语义：重发 prompt 含已生成
token）——模式 B/C 下客户端可见流本身即完整序列，检测累积天然覆盖全部 token；模式 A 下由
`on_recompute` 重置累积器，仅最终成功流完整检测一次。
