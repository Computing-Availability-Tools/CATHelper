# 推理精度异常检测中间件 设计文档(DESIGN)

## 1. 概述

### 1.1 设计目标

构建一个纯 ASGI（Asynchronous Server Gateway Interface） 中间件包 `anomaly_middleware`，通过 vLLM 的
`--middleware` 插件部署：

```
vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware
```

中间件对客户端**透明**：拦截推理请求、强制采集 logprobs和token_id、后台运行算法异常检测、不影响客户端请求响应状态返回、并通过独立 Prometheus 端点暴露检测结果。客户端完全感知不到中间件的存在。

除单机模式外，组件还支持 **PD 分离（Prefill/Decode Disaggregation）部署形态**：检测能力
上移至服务化代理（load_balance_proxy）集中部署，作为全局唯一检测点，P/D 节点启动命令
保持纯净（详见 §9 与 `design_pd_proxy.md`）。

### 1.2 设计原则

- **透明优先**：`enabled=True` 和异常监控概率共同作用，决定请求是否注入、响应恢复和检测。但不影响客户端看到的响应。
- **流式不缓冲**：采用纯 ASGI（而非 Starlette `BaseHTTPMiddleware`），
  SSE 流增量转发，避免缓冲破坏流式语义。
- **检测不阻塞客户端**：检测在响应全部发送完毕后以 fire-and-forget
  方式调度，客户端永远不等待检测。
- **硬依赖启动报错**：`configs/detector.yaml`、env 变量、ILLDetector 构造、tokenizer
  加载等启动期硬依赖失败 → 终止服务启动，避免"静默降级运行"导致用户不知晓检测已失效。
- **推理期降级**：检测执行异常、单 token decode、进程池崩溃等推理期错误 → log +
  不影响推理 + 不降级算法。
- **检测优先**：检测出错只影响当次检测（log + 计 error），不影响后续请求的检测能力，
  不设 `enabled=False`，不标记全局不可用。
- **不改写检测算法逻辑**：检测器接口从 `List[Dict[int,float]]` 改为 numpy 数组，
  仅改数据访问方式，阈值/FFT/滑窗/n-gram 等算法逻辑完全不变。
- **单一统一类**：一个中间件类持有全部不可变配置与状态，按请求构造
  轻量协作者，避免多层包装与跨类共享可变状态。

### 1.3 不做的事

- 不改写检测算法本体、不新增异常类型。
- 不替换或干扰 vLLM 自带 `/metrics`。
- 不支持非 vLLM 的 ASGI 服务器（代码可移植，但仅针对 vLLM 请求/响应形态测试）。

## 2. 总体架构

### 2.1 部署形态

`--middleware <module.path>.<ClassName>` 由 vLLM 经 `importlib`/`getattr`
（点分隔）加载，因值为类而实例化为 `Cls(app)`——**无 kwargs、无启动钩子、
无路由注册钩子**。由此导出四条硬约束：

1. 构造签名固定为 `__init__(self, app)`；
2. 全部配置来自环境变量/磁盘文件；
3. 重活（numpy、tokenizer、检测器、进程池）须在 `__init__` 同步完成——无启动钩子，
   `__init__` 即唯一初始化时机，启动期 fail-fast（硬依赖失败即终止）；
4. 指标端点必须由中间件**内联**响应（不能用 `app.add_api_route`）。

### 2.2 模块组成

```
project_root/
├── pyproject.toml           # 项目配置（包名 anomaly_middleware）
├── conftest.py              # pytest 根配置（sys.path 设置）
├── configs/
│   └── detector.yaml        # 检测器算法默认参数
│   └── webui.yaml           # Web 信息配置
├── tests/                   # 单元测试 + 端到端测试
├── webui/                   # Web 精度可视化监控
├── docs/                    # 设计文档 + 规格 + README
├── tools/                       # 辅助工具脚本
│   ├── gen_token_category.py    # 离线生成词表类别映射 + token 文本表（PD 模式，§9.3）
│   └── run_proxy_with_anomaly.py # PD 模式独立启动器：零修改运行 proxy 脚本并附加检测（§9.3）
└── anomaly_middleware/          # Python 包
    ├── __init__.py            # 重导出 AnomalyMiddleware / ResponseInterceptor / RequestContext
    ├── middleware.py          # 统一中间件类 + RequestContext + ResponseInterceptor + eager 初始化
    ├── env.py                 # 处理环境变量
    ├── logging.py             # 日志格式
    ├── metrics.py             # 独立 CollectorRegistry + 指标记录/渲染
    ├── extractor.py           # 抽取/恢复（流式与非流式）+ SSEStreamProcessor
    ├── token_resolver.py      # TokenTextResolver + tokenizer 获取（env/argv/缓存自动发现）+ parse_vllm_argv
    ├── token_categorizer.py   # token 分类纯函数 + 启动期 generate_tk2cat（§3.11）
    ├── detector.py            # ILLDetector 检测器本体（set_vocabulary + topk_n 参数）
    ├── detector_runner.py     # DetectorRunner（进程池+共享内存+调度+词表注入）
    ├── anomaly_store.py       # 异常信息本地保存（编号分配 + pickle 落盘，§3.12）
    └── proxy_integration.py   # PD 模式 proxy 集成层（§9.3）：ProxyAnomalyMiddleware /
                                # ProxyAnomalyController / ProxyStreamProcessor / StaticTokenTextResolver
```

### 2.3 职责划分

| 组件 | 职责 |
|---|---|
| `AnomalyMiddleware` | 持有配置/runner/指标/待办任务集；`__call__` 分派：内联指标、降级透传、拦截注入、装拦截器、委托下游；`__init__` 内 eager 初始化 |
| `RequestContext` | 单请求上下文：原始参数、model、关联 id、是否检测 |
| `ResponseInterceptor` | 包装 `send`：判流式/非流式、注入关联头、缓冲或增量处理、恢复响应、调度检测（写共享内存 + 提交元数据） |
| `SSEStreamProcessor` | 跨块事件重组、每块恢复、per-choice 累积检测数据 |
| `Extractor` | 纯函数：解析 token-id、抽取 per-choice (logprobs,token_ids) numpy 数组、恢复响应（截断/null/文本还原，经 `_token_text` 统一走 resolver 优先） |
| `DetectorRunner` | 多进程池（`ProcessPoolExecutor`）+ 共享内存零拷贝；`set_vocabulary` 经 initializer 注入；`run_async(metadata)`；进程池崩溃恢复 |
| `TokenTextResolver` | token_id(int)→单 token surface 文本（`decode([id])`）；进程级单例，启动期同步构造，失败 raise；tokenizer 获取经 env/argv/缓存自动发现 |
| `TokenCategorizer` | token 分类纯函数（`categorize_token`）+ 启动期 `generate_tk2cat(tokenizer)` 生成 `{token_id: category}` 映射（§3.11） |
| `ILLDetector` | 检测器本体：`set_vocabulary` 接受启动期映射；`topk_n` 参数消除首次锁定；`get_tk2cat` 返回映射或 (None,None) 降级 |
| `PluginConfig` | 环境变量读取与校验（含 `detector_workers`）；检测器路径固定 `configs/detector.yaml` |
| `Metrics` | 独立 registry；计数/直方图/gauge；渲染文本暴露 |

PD 分离模式（§9）新增组件：

| 组件 | 职责 |
|---|---|
| `ProxyAnomalyMiddleware` | PD 模式 ASGI 中间件：继承单机版 `AnomalyMiddleware`，完整复用其 `__call__`；仅替换初始化——无 tokenizer，启动期加载预生成 tk2cat + 文本表文件（fail-fast） |
| `ProxyAnomalyController` | PD 模式 handler 挂钩控制器：`before_forward`（采样+快照+注入）→ `feed_stream`（流处理+事件上抛）→ `on_recompute`（重置累积）→ `finish`（收尾+调度检测） |
| `ProxyStreamProcessor` | PD 模式流处理器：SSE 重组 + strip 恢复 + 检测数据累积 + 解析事件上抛（复用 `SSEStreamProcessor`）；非流式字节缓冲 + 整体 extract/strip |
| `StaticTokenTextResolver` | 预生成 token 文本表查表（与 `TokenTextResolver.resolve` 同接口，strip 调用侧零改动） |

## 3. 核心功能设计

### 3.1 请求拦截与参数注入

**拦截范围**：仅 `/v1/chat/completions` 与 `/v1/completions`。
所有其他 HTTP 请求（任意方法或路径）均原样转发给下游应用（保持原先vllm处理方式一致）。

**请求侧 ASGI 契约**：
1. **读请求体**：`_read_all_body(receive)` 循环 `receive()`，聚合全部 `http.request`
   body 至 `more_body=False`；遇 `http.disconnect` 中止。
2. **重放 receive**：`_make_replay_receive(original_receive, body, request_id)` 包装——
   首次调用返回合成单条 `{"type":"http.request","body":body,"more_body":False}`；
   **后续调用委托原始 `receive()`**（返回 `http.disconnect` 等真实后续消息）。
   ⚠ 二次读**禁止**返回空 body 的 `http.request`：vLLM 会将其视为新请求而重复
   处理/重复下发（实测缺陷）。透传与注入两条路径都要重放已消耗的 body。
3. **请求 scope**：`_patch_scope_content_length(scope, len)` 浅拷贝 scope，
   改写/补 `content-length` 为注入后新 body 长度。

**强制注入**（覆盖请求体以加上检测所需参数）：
- chat：`logprobs=true`、`top_logprobs=<注入值>`、`return_tokens_as_token_ids=true`
- completions：`logprobs=<注入值>`（此处 logprobs 即数量）、`return_tokens_as_token_ids=true`
- **注入值 = max(客户端原始值, N)**：客户端带 `top_logprobs`（chat）/
  `logprobs`（completions）=M 且 M>N → 注入 M，保证每 token 有 M 项数据（检测截断见
  §3.2）；否则注入 N。例：客户端 `top_logprobs=5`、N=20 → 注入 20；
  客户端 `top_logprobs=10`、N=5 → 注入 10（见 spec §2.2）。
- `return_tokens_as_token_ids` 始终注入 `true`；客户端未带 → 恢复其默认 `false`。

**快照**（`save_original_params`）：注入前缓存客户端原始 `logprobs`/
`top_logprobs`/`logprobs`、`return_tokens_as_token_ids` 与 **`n`** 等采集参数，供
§3.2 响应恢复。注入后修正请求 `Content-Length` 匹配新 body 长度（否则下游解析长度
不匹配导致截断/挂起）。

**关键不变量**：`top_logprobs` 跨请求必须恒定（默认 20，可配置 1-20）。
原因：保证每 token 的 top-logprobs 条目数一致，检测语义稳定（§6.2）。
`topk_n` 由参数传入检测器，不再依赖实例态锁定（§4.5）。

### 3.2 响应抽取与恢复

**抽取**（供检测）：每个 choice 取 `(logprobs: np.ndarray, token_ids: np.ndarray)`
（per-choice numpy 数组；`tokens` 不再单独提取，检测器内部取 `token_ids[:, 0]` 作为输出
token 序列，见 §4.5）。**已实测确认**：注入 `return_tokens_as_token_ids=true` 后
chat/completions 各 token 字段确为 `"token_id:NNN"`。内部遍历 JSON 时收集 token_id + logprob
到列表，最后一次性 `np.array()` 构建；`_truncate_topk` 改为 numpy 排序+截断，产出**已降序排列**
的数组（`np.argsort(kind='stable')`）。
- chat：logprobs 位于 `choices[].logprobs.content[]`。每 entry 的
  `token` = `'token_id:NNN'`（解析为 int）；`top_logprobs[]` 为对象列表，每项
  `TopLogprob(token='token_id:1122', bytes=[22,33,55], logprob=-0.3)`——含独立
  `token` 字段（同解析为 key）与 `logprob` 值。
- completions：logprobs 位于 `choices[].logprobs`：
  `tokens[]`（`'token_id:NNN'` 字符串序列）解析为 token-id 序列；
  `token_logprobs[]` 为与 `tokens` 平行的 logprob 数值列表；
  `top_logprobs[]` 为与 `tokens` 平行的 dict 列表（token-id 字符串→logprob），
  解析为 dict[int,float] 后转 numpy 数组。

`parse_token_id` 兼容两种形态：`"token_id:NNN"` 与纯数字串（如 `"22"`），失败返回 -1。

**恢复**（供客户端，按原始参数，统一走 `_token_text` 规则，见 §3.10）：
- 客户端未请求 `logprobs` → `choice.logprobs=null`。
- 客户端请求 `logprobs=True`、`top_logprobs=n`（chat）/`logprobs=n`（completions）→ 截断到 n。
- 客户端未请求 `return_tokens_as_token_ids` → token_id→文本还原（§3.10）：
  - 统一规则 `_token_text(token_id, bytes, resolver, *, fallback_to_id=False)`：**resolver 优先** `decode([id])`
    （resolver 启动期已就绪，见 §3.10）；个别 decode 未解析 → 退回 `bytes`（仅当解码出真实文本且不含
    `token_id:` 前缀）；都无 → `fallback_to_id=True` 时回退 `token_id:NNN`（§4.7 例外），否则 null。
  - chat `content[].token` / `top_logprobs[].token`：resolver 覆盖所有字段；个别 decode 失败时主 token
    退回 bytes（三层第二层），top_logprobs 跳过破损 bytes（三层第二层失败）→ `fallback_to_id=True` 时落
    `token_id:NNN`（三层第三层），否则 null。
  - completions `tokens[]` / `top_logprobs[]`：无 bytes，resolver 可用时还原真实文本、个别 decode 失败且
    `fallback_to_id=True` 时回退 `token_id:NNN`、否则 null；`top_logprobs[]` 重建为 `{文本或token_id:NNN:logprob}`。
- 客户端**已**请求 `return_tokens_as_token_ids` → 原样保留 `token_id:NNN`（这正是客户端所要）。

> **背景**：vLLM 在 `return_tokens_as_token_ids=true` 下，chat `top_logprobs` 的 `bytes` 填的是
> token_id 字符串本身的字节（非 token 真实字节）→ `_decode_bytes` 会泄漏 `token_id:`；
> completions 响应形态本就无 `bytes` → 仅靠 bytes 路径无法还原文本。故引入 tokenizer `decode([id])`
> 为统一文本来源，bytes 仅作个别 decode 失败时的兜底（带泄漏守卫）。resolver 为启动期硬依赖
> （加载失败即终止服务），故运行期恒可用；`fallback_to_id` 的触发条件 `resolver is None` 不再命中
> （§4.7 例外保留为防御性代码）。

**检测截断与客户端截断分离**：注入值为 `max(客户端, N)` 时每 token 的 top-logprobs
条目数可能 > N。抽取检测数据（`extract_*_response`）时每 token **截断至 N**（检测器
`topk=N` 截断，见 §4.5）；恢复给客户端时**截断至客户端请求值**。例：客户端 `logprobs=10`、
N=4 → 注入 10、每 token 10 项；送检测截前 4 项，返回客户端 10 项（见 spec §2.3）。

**多候选（`n>1`）**：客户端设置 `n` 被快照保留；抽取/恢复/检测按 choice 循环处理 n 份
候选，客户端输出逐 choice 套用上述规则（见 spec §2.3）。

### 3.3 流式响应处理（SSE）

**流式形态**（**已实测确认**）：vLLM 流式每块只含**最新 token** 的 logprobs 和 token_id。
设计要求**先缓存全部流式推理结果，再进行检测**。

- chat 流式：logprobs 位于 `choices[].logprobs.content[]`（**非 delta**），每块含本块
  新 token 的一个 entry（`token='token_id:NNN'`、`logprob`、`bytes`、`top_logprobs[]`
  对象列表，形态见 §3.2）。累积器对每块 content 的每个 entry 做 append。
- completions 流式：logprobs 位于 `choices[].logprobs`，每块 `tokens[]`/
  `token_logprobs[]`/`top_logprobs[]`（形态见 §3.2），append。
  - 防御：若某块 `tokens`/`top_logprobs` 呈现累积数组（位置与已累积重叠），采用
    **latest-longest-wins**（取最长/最新数组覆盖），兼容可能的累积式。默认按 delta-append。

**SSE 处理状态机**（`SSEStreamProcessor`）：
- 跨块缓冲 `_buffer`，按 `\n\n` 切分完整事件；半事件留缓冲。
- `_process_event`：分离 `data:` 行与其它行（`event:`/`id:`/`retry:`/注释）；
  无 data 行（keep-alive）原样透传；`data: [DONE]` 原样透传；其余 `json.loads` 成功则
  捕获 model → `_extract_streaming`（per-choice append 累积）→ `_strip_streaming`
  （每块无状态恢复）→ 重序列化 `data: <json>\n` + 其它行 + `\n\n`。
- `flush()`：排空尾部残余（无 `\n\n`），处理之，输出则补 `\n\n`。
- `get_detection_data()`：按 choice index 升序返回 `(logprobs_all, token_ids_all)`
  （per-choice numpy 数组列表）。

**双态并存**：转发是增量无状态的（每块即发），检测数据累积是有状态的（跨块 append）。
两者读写不同字段，互不干扰。`[DONE]`/keep-alive 永不参与累积。

**CRLF 兼容**：SSE 规范允许 `\r\n\r\n`；`_process_event` 内对每行 `rstrip(b"\r")` 兼容。

### 3.4 异常检测调度

- 检测在**响应全部发送完毕后**调度（fire-and-forget）：非流式在终端 body 发出后；
  流式在 `[DONE]`/`more_body=False` 后。
- 检测数据：非流式取 `extract_*_response` 的 per-choice `(logprobs, token_ids)` 数组；
  流式取 `SSEStreamProcessor.get_detection_data()`。
- **空响应不检测**：`token_ids` 为空或全空时跳过。
- 检测任务防 GC：中间件持 `_pending_tasks: set`，入集，`done_callback` 出集；
  关闭时未完成任务随 event loop 取消（fire-and-forget 可接受）。
- 异常全捕获：检测协程内 try/except，失败计 `detection_errors_total`，不影响客户端。

### 3.5 异常监控概率

- 异常监控概率方法：每目标请求抽 `rand = random.random()`；
  `will_detect = rand < monitor_rate`（默认 1.0，范围 0-1）。
- 未选中（`will_detect=False`）→ **纯透传**：不读 body、不注入、不恢复、不检测，
  原样转发给下游（spec §2.8）。
- 选中 → 该请求完整走读 body→注入→恢复→检测链路。`monitor_rate=0` 永不注入不检测；
  `1.0` 全检测。
- **运行时动态更新**：`monitor_rate` 启动期由 env 初始化为 `self._monitor_rate`，
  运行时可通过 `POST /anomaly/config` 端点动态更新（见 §3.13）。采样判断读
  `self._monitor_rate`（内存属性），非 `config.monitor_rate`（启动快照）。
  重启回退 env 初始值。


### 3.6 请求关联标识

- 每被拦截请求生成 `request_id = uuid.uuid4().hex`。
- 在 `http.response.start` 追加响应头
  `(b"x-anomaly-request-id", request_id)`，**然后再发给下游 send**。
  - 流式：start 立即发送→注入后再发。
  - 非流式：start 本就缓冲→发前注入（同时可 patch 响应 Content-Length）。
- `request_id` 传入检测任务用于日志，支持端到端追踪。

### 3.7 Prometheus 指标

- `__call__` 在最前拦截 `GET <metrics_path>`（默认 `/anomaly/metrics`），直接内联响应，
  不涉及下游路由。仅 GET 被拦截；POST 到该路径透传给下游。
- 独立 `CollectorRegistry`（与 vLLM 默认 `/metrics` 隔离）。
- 指标：
  - `vllm_anomaly_requests_total`（Counter）
  - `vllm_anomaly_detected_total`（Counter，labels `ill_type`,`model`）
  - `vllm_anomaly_detection_errors_total`（Counter）
  - `vllm_anomaly_detection_duration_seconds`（Histogram）
  - `vllm_anomaly_last_result`（Gauge，labels `ill_type`,`model`）
- `ill_type` 取值：0=normal,1=rare_character,2=garbled,3=repetition,4=nan_value。
- `normal`(0) 只增 requests，不计 detected。
- Content-Type：`text/plain; version=0.0.4; charset=utf-8`。
- `model` 标签来自请求体 `model` 字段；缺失用 `"unknown"`。
  - `vllm_anomaly_monitor_rate`（Gauge）：当前异常监控概率（运行时可更新）。

### 3.8 配置与路径解析

环境变量（带默认）：
- `VLLM_ANOMALY_ENABLED`（默认 1）
- `VLLM_ANOMALY_MONITOR_RATE`（默认 1.0，范围 0-1）：请求被异常监控的概率。
  `0` 永不注入不检测；`1.0` 全检测。
- `VLLM_ANOMALY_TOP_LOGPROBS`（默认 20，范围 1-20）
- `VLLM_ANOMALY_METRICS_PATH`（默认 `/anomaly/metrics`）
- `VLLM_ANOMALY_DETECTOR_WORKERS`（默认 4，范围 ≥1）：检测进程池 worker 数，建议范围 1-16。
- `VLLM_ANOMALY_TOKENIZER_MODEL`（默认未设）：显式 tokenizer 加载源（最高优先），设为
  `vllm serve --model` 的实际值或 `--tokenizer` 的值（本地目录路径或 HF repo id）。覆盖 served 名为裸 basename /
  本地目录部署。未设则自动从同进程 `sys.argv` 解析 `vllm serve` 命令行：
  `--tokenizer` → `--model` 位置参数 → HF 缓存扫描（§3.10，不含 HTTP loopback）。
- `VLLM_ANOMALY_SAVE_PATH`（默认未设）：异常信息本地保存路径（绝对路径）。未设→不落盘，
  仅维护内存异常编号；设置且以 `.pkl` 结尾→文件模式；否则→文件夹模式（文件名=`<served_model_name>.pkl`）。
  启动期校验目录/父目录存在性（fail-fast），见 §3.12。

校验（启动期）：`top_logprobs∈[1,20]`、`monitor_rate∈[0.0,1.0]`、`detector_workers≥1`。
越界 → raise 终止服务启动（含中文错误提示，见 §3.9）。

检测器配置路径固定为 `configs/detector.yaml`（项目根目录），不可通过 env 覆盖。
文件不存在 → `resolve_config_path()` raise（启动期硬依赖，见 §3.9）。
- `VLLM_ANOMALY_CONFIG_PATH`（默认 `/anomaly/config`）：配置端点路径，用于
  运行时动态更新 `monitor_rate`。

### 3.9 降级机制

降级分启动期与推理期两类，启动期硬依赖失败直接报错终止（fail-fast），推理期错误才走降级：

**启动期 fail-fast**（`__init__` 内，`enabled=True` 时）：
- `configs/detector.yaml` 缺失 / env 变量越界 / ILLDetector 构造失败 / tokenizer 加载失败
  → 任一步骤失败直接 raise 终止服务启动，含中文错误提示（见 §4.1 启动期 eager 初始化流程）。
- `enabled=False` → 跳过全部启动检查，纯透传模式（指标端点仍可达报零值），无需
  detector/tokenizer/进程池。

**启动期软降级**（仅 tk2cat）：
- tk2cat 生成失败 → 无词表检测（算法内置 `get_tk2cat()→(None,None)` 降级路径），
  rare/garbled 走 top1 logp，repetition/acf/trajectory 不受影响；记 WARNING，服务正常启动。

**推理期降级**（不影响整个推理过程，遵循"检测优先"）：
- 单请求检测执行异常（`detector.run()` 抛错）→ `schedule_detection` 的 `_run()` catch →
  log（含 request_id/model/异常详情）+ 计 `detection_errors_total`；不设 `enabled=False`，
  不标记 runner 不可用，后续请求继续检测。
- 单 token `decode([id])` 抛错 → `resolve()` 返回 None，不终止推理，其他 token 正常还原。
- 无运行事件循环（`asyncio.create_task` RuntimeError）→ log + 跳过**该次**检测，
  下一个请求照常尝试，不影响后续检测能力。
- 进程池崩溃（`BrokenProcessPool`）→ 重建进程池 + 该请求计 error + log，后续请求在新进程池上
  正常检测（见 §4.4）。

**`enabled=False` 行为**：`__call__` 早退透传（不读 body、不注入、不拦截），
指标端点仍可达报零值。总开关关闭 = 纯透传，不校验 detector/tokenizer/进程池。

### 3.10 token 文本还原（TokenTextResolver）

**背景与问题**：中间件强制注入 `return_tokens_as_token_ids=true`，使响应 token 字段呈
`"token_id:NNN"`（供检测抽取 token_id）。客户端**未**请求 `return_tokens_as_token_ids` 时，
中间件须把 `token_id:` 还原为 token 文本回客户端（spec §2.3、不变量 #7「`token_id:` 限制」）。
`_decode_bytes` 从 `bytes` 字段解码仅能覆盖 chat 主 token（实测正确）；对 chat `top_logprobs[].token`
会泄漏 `token_id:`（vLLM 把 token_id 字符串本身的字节塞入 `bytes`），对 completions 各字段无 `bytes`
可解。根因：`bytes` 路径无法覆盖这两种情形，须引入真正的 tokenizer 做 `decode([id])`。

**职责与接口**：`TokenTextResolver.resolve(token_id: int) -> Optional[str]`——给定 token_id 返回
该 token 的 surface 文本（OpenAI 语义即 `decode([id])`），不可用返回 `None`（调用方置 null）。
仅被 ASGI 事件循环（strip 路径）调用；检测 worker 进程不调用（检测用 token_id 整数）。
进程内单例、启动期同步加载、全请求复用。

**tokenizer 获取顺序**（启动期 `__init__` 内同步执行，`acquire_tokenizer(explicit)`，不含 HTTP loopback）：

1. **显式 env `VLLM_ANOMALY_TOKENIZER_MODEL`**（最高优先）：设为 `vllm serve --model` 实际值
   或 `--tokenizer` 的值（本地目录路径或 HF repo id）→ `from_pretrained(explicit, local_files_only=True)`。
   覆盖本地目录部署（served 名为裸 basename、不在 HF 缓存、from_pretrained 与缓存扫描均无法解析）。
   未设则跳过。
2. **`--tokenizer` 从 `sys.argv` 解析**（`parse_vllm_argv()`）：vLLM 启动命令中 `--tokenizer <path>`
   即 vLLM 实际使用的 tokenizer 路径——与 `--model` 不同时（如使用独立 tokenizer），此为最精确来源。
   `parse_vllm_argv()` 解析 `vllm serve <model> ... --tokenizer <path> ... --host H --port P`，
   支持 `--flag value` 与 `--flag=value` 两种形式；非 `serve` 命令返回 None。
3. **`--model` 位置参数从 `sys.argv` 解析**：无 `--tokenizer` 时，`vllm serve <model>` 的 `<model>`
   即 tokenizer 路径（vLLM 默认从模型目录加载 tokenizer）。`parse_vllm_argv()` 提取 serve 后首个
   位置参数（跳过 `_VALUE_FLAGS` 中已知带值 flag 的值，避免误识别）。
4. **HF 缓存扫描**：以 argv `--model` 名为 hint，served/model 名为裸 basename（如 `Qwen3-0.6B`）
   而 HF 缓存键为完整 repo id（如 `Qwen/Qwen3-0.6B`）时，`huggingface_hub.scan_cache_dir()` 找
   `repo_id` 以 `/<hint>` 结尾或等于 `<hint>` 的条目（短优先），补全后重试 `from_pretrained`。
   `huggingface_hub` 不可用 → 返回 []。
5. 均失败 → raise（终止服务启动），错误信息提示用户显式设置环境变量
   `VLLM_ANOMALY_TOKENIZER_MODEL` 为 `vllm serve <model>` 的实际值（或 `--tokenizer` 的值）。
   启动期硬依赖，不软降级（服务拉起前能加载则正常，否则终止）。


**文本缓存**：`resolve` 内部维护 `dict[int, str]` 缓存，首次 `decode([id])` 后存入，后续命中微秒级。
缓存仅被事件循环单线程访问，无需锁；容量上界为实际出现过的 token id 数（远小于词表）。

**生命周期与触发点**：resolver 进程级、启动期同步构造（`__init__` 内 `acquire_tokenizer`
完成后立即构造 `TokenTextResolver`），无懒加载、无双检锁、无预热线程。失败 → raise 终止
服务启动（启动期硬依赖，见 §3.9）。`AnomalyMiddleware.shutdown()` 无特殊清理（tokenizer
随进程退出）。

**strip 路径统一规则**（`extractor.py` `_token_text(token_id_value, bytes_value, resolver, *, fallback_to_id=False)`）：

```python
def _token_text(token_id_value, bytes_value, resolver, *, fallback_to_id=False):
    # 1) 优先 resolver：覆盖 chat 主 token / top_logprobs / completions 全部字段
    if resolver is not None:
        tid = parse_token_id(token_id_value)
        if tid >= 0:
            txt = resolver.resolve(tid)
            if txt is not None:
                return txt
    # 2) resolver 缺失 / 未解析 → 退回 bytes（仅当解码出真实文本，不含 token_id: 前缀）
    if bytes_value is not None:
        s = _decode_bytes(bytes_value)
        if s is not None and not s.startswith(TOKEN_ID_PREFIX):
            return s
    # 3) 都无 → §4.7 降级例外：fallback_to_id=True 时回退 token_id:NNN，否则 None
    if fallback_to_id:
        tid = parse_token_id(token_id_value)
        if tid >= 0:
            return f"{TOKEN_ID_PREFIX}{tid}"
    return None
```

要点：步骤 1 优先 resolver，文本来源统一、一致；步骤 2 的 `not s.startswith(TOKEN_ID_PREFIX)`
守卫**独立修复泄漏**——个别 decode 失败时，chat top_logprobs 的破损 bytes 也被识别并跳过，
不再泄漏 `token_id:`；步骤 3 为 §4.7 降级例外——`fallback_to_id=True` 时回退 `token_id:NNN`。
`fallback_to_id` 由 strip 函数按触发条件计算（见下），默认 `False` 维持 null 行为。
resolver 为启动期硬依赖（加载失败即终止服务，见 §3.10），运行期恒非 None，故触发条件中
`resolver is None` 不再命中——`fallback_to_id` 恒为 `False`，§4.7 例外保留为防御性代码。

**触发条件**（`strip_chat_response` / `strip_completions_response` 头部各算一次）：
- chat：`not orig.return_tokens_as_token_ids and resolver is None and orig.logprobs is True and (orig.top_logprobs or 0) > 0`
- completions：`not orig.return_tokens_as_token_ids and resolver is None and (orig.logprobs or 0) > 0`

> resolver 启动期同步构造且为硬依赖（§3.10），运行期恒非 None，上述触发条件不再命中——
> `fallback_to_id` 恒为 `False`。个别 token 的 `decode([id])` 失败仍走步骤 2（bytes 兜底）→ 步骤 3（null）。

**三层兜底**（chat 主 token）：resolver → bytes（真实文本）→ `token_id:NNN`（`fallback_to_id=True` 时）。
bytes 仍优先用——vLLM 已填了主 token 的真实 bytes，不用客户端自解码；仅在 bytes 破碎
（解码出 `token_id:` 前缀，守卫拒绝）时落 `token_id:NNN`。chat top_logprobs 的 bytes 是
`token_id:` 字符串本身字节（破损），故三层第二层必失败。completions 无 bytes 字段，二层直接跳过。
resolver 恒可用时第一层即返回，二三层仅个别 decode 失败时触发。

**行为变更**（相对引入前）：① chat `top_logprobs[].token`：原泄漏 `"token_id:NNN"` → resolver
可用时真实文本（**bug 修复**）；② completions `tokens[]`/`top_logprobs[]`：原恒 null
→ resolver 可用时真实文本；③ §4.7 降级例外：触发条件命中（客户端请求 topk + 未设 rtati +
`resolver is None`）时受影响字段由 null 改为 `token_id:NNN`——resolver 启动期硬依赖化后此条件
不再命中，保留为防御性代码；④ chat 主 token：文本来源由 bytes 改为 **resolver 优先**
（个别 decode 失败时退回 bytes），可观察文本值不变。

### 3.11 token 分类与词表注入

检测器的生僻字（rare_character）与乱码（garbled）检测依赖 token 到类别的映射
（`tk2cat`：`{str(token_id): category}`），用于判断输出中是否出现非常规字符类别
（生僻 CJK / 乱码符号 / 控制字节等）。映射在启动期从已加载 tokenizer 同步生成，
不依赖预生成文件。

**token 分类**（`token_categorizer.py` `categorize_token`）：对单个 token 的解码文本
逐字符做 Unicode 脚本分类（`_classify_char`，`lru_cache` 加速），统计各类别占比，
取主导类别映射为类别标签（如 CJK→`chinese_cjk`、拉丁→`english_latin`、数字→`numbers`、
符号密集→`gibberish_symbols`、控制字节→`control_bytes` 等）。纯函数、无副作用，
供检测器与中间件共享。

**运行时映射生成**（`generate_tk2cat(tokenizer) -> (id_to_category, vocab_size)`）：
1. `tokenizer.get_vocab()` 取词表 → `invert_vocab` 反转为按 index 排序的 token 字符串列表；
2. decode 降级链（`_get_decode_fn`）优先 `backend_tokenizer.decoder.decode([token])`
   （最精确），退到 `tokenizer.decode([idx])`（高层 API）；均无则 raise（调用方降级）；
3. 逐 token `_safe_decode`（异常吞掉、跳过该 token）→ `categorize_token` →
   `{str(token_id): category}`。

**启动期同步生成**：`AnomalyMiddleware.__init__` 内 tokenizer 加载完成后（§3.10）立即同步
调用 `generate_tk2cat(tokenizer)` 生成映射。生成失败 → **软降级**（tk2cat=None，无词表
检测模式），记 WARNING，服务正常启动。无预热线程、无懒加载、无首请求慢路径补生成。

**注入**：tk2cat 在启动期固定后，经 `DetectorRunner` 的 `ProcessPoolExecutor` initializer
（`_worker_init`）注入每进程 worker——建池时随 `initargs=(config_path, tk2cat, vocab_size, topk_n)`
传入，每进程构造 `ILLDetector` 后立即 `set_vocabulary`（若 tk2cat 非 None）。运行期不再
调用 `set_vocabulary`（如需更新则重建进程池，当前设计无此需求）。

**降级**：检测器 `get_tk2cat()` 返回预计算映射或 `(None, None)`；后者降级为**无词表检测**：
rare/garbled 走 top1 logp 路径（按概率阈值判异常），repetition/acf/trajectory 不受影响。
tk2cat 为 None 时 worker 的 `set_vocabulary` 不调用，检测器走无词表路径。

### 3.12 异常信息本地保存

当某被检测请求的某候选检出异常（`is_ill and ill_type != 0`）时，把异常现场保存为本地
pickle 文件（dict，key=异常编号，value=异常信息）。环境变量 `VLLM_ANOMALY_SAVE_PATH` 控制
是否落盘；不设置则不落盘，异常编号仍由内存计数器累加（重启归零）。

**配置**（`env.py`）：新增 `VLLM_ANOMALY_SAVE_PATH`（绝对路径）。值以 `.pkl` 结尾→文件模式
（路径即文件名）；否则→文件夹模式（文件名=`<served_model_name>.pkl`，served 名经
`parse_vllm_argv().model` 净化）。非绝对路径→启动期 raise。启动期 fail-fast：文件夹模式目录
必须预先存在；文件模式父目录必须预先存在；文件本身首次保存创建（`AnomalyStore.__init__` 校验）。

**AnomalyStore**（新增 `anomaly_store.py`）：异常编号分配 + pickle 落盘 + 内存镜像 `_data`。
- 编号：落盘开启时首次保存读文件取 `max(内存计数器, max(int keys))` 同步计数器后累加（防重启覆盖）；
  关闭时仅内存计数器累加。编号自增不回退。
- 保存执行：编号自增 + `_data` 更新在事件循环内（锁内、瞬时、原子）；磁盘写经
  `loop.run_in_executor` offload 到线程，`asyncio.Lock` 串行化，事件循环不阻塞、客户端不受影响。
  首次读文件做编号同步是一次性同步读。
- 降级：保存失败（IO/权限/pickle）→ catch + log，不影响客户端/检测/后续请求，不回退编号，
  不设不可用（遵循「检测优先 / 推理期降级」）。

**数据采集**：`prompt` 在 `__call__` 从请求体取（chat `messages` / completions `prompt`），存
`RequestContext.prompt`；`text` 在 `ResponseInterceptor` 取——非流式 `choices[].message.content` /
`choices[].text`，流式 `SSEStreamProcessor` 累积 `delta.content` / `text`（`get_choice_texts()`，
按 choice index 升序与 `get_detection_data` 对齐）。

**检测调度**（`detector_runner.py`）：`schedule_detection` 增 `anomaly_store`/`prompt`/`texts`
三个可选 kw（向后兼容）。`_run()` 在检测计时之外（不计入检测耗时）遍历异常候选，构建记录
（`topk_logprobs`/`tokens_ids` 用 `.tolist()`），`await anomaly_store.save(record)`，
`metrics.record_anomaly(aid, ts, model)`。

**指标**（`metrics.py`）：新增 `vllm_anomaly_last_id{model}` 与
`vllm_anomaly_last_timestamp_seconds{model}` 两个 Gauge，**不改动现有指标/标签**（webui
collector 只解析已知指标，不受影响）。

### 3.13 动态配置端点

中间件在 `__call__` 内联拦截配置端点（同 metrics 端点模式，无 `app.add_api_route`）。
路径由 `VLLM_ANOMALY_CONFIG_PATH` 配置（默认 `/anomaly/config`）。

- **GET** `/anomaly/config`：返回当前 `{"monitor_rate": <float>}`。
- **POST** `/anomaly/config`：接收 `{"monitor_rate": <float>}`，校验 ∈ [0.0, 1.0]，
  更新 `self._monitor_rate` + `vllm_anomaly_monitor_rate` gauge，返回 `{"monitor_rate": <float>}`。
  校验失败返回 400 + `{"error": <msg>}`，`_monitor_rate` 不变。
- 其他 method → 透传。
- `enabled=False` 时端点仍可达（同 metrics 端点）。
- 无认证（同 metrics 端点，vLLM API 本身无认证层）。
- 不持久化：重启回退 env 初始值。

## 4. 关键组件设计

### 4.1 AnomalyMiddleware（统一中间件类）

**构造** `__init__(self, app)`：建 `PluginConfig`（`from_env`，env 变量非法 → raise
终止服务启动）；attach 指标助手；建 `_pending_tasks` set。
   `self._monitor_rate = self.config.monitor_rate`（运行时可变属性，初始值来自 env）；
   `self.metrics.set_monitor_rate(self._monitor_rate)`（gauge 初始化）。
若 `enabled=True` 则**同步**
完成以下 eager 初始化步骤（任一步骤失败直接 raise 终止服务启动，见 §3.9）：

1. `resolve_config_path()` → 检查 `configs/detector.yaml` 存在；不存在 raise（提示文件路径）。
2. 加载 tokenizer（同步，路径顺序见 §3.10：env → argv `--tokenizer` → argv `--model` → HF 缓存扫描）；
   全部失败 raise（提示设置 `VLLM_ANOMALY_TOKENIZER_MODEL`）；成功 → 构造 `TokenTextResolver`。
3. `generate_tk2cat(tokenizer)` 生成词表映射；失败 → 软降级（tk2cat=None，无词表检测，记 WARNING）。
4. eager 构造 `ILLDetector(config_path)`（主进程，验证 numpy 可用 + config 解析）；
   失败 raise（提示 numpy 缺失 / yaml 格式 / 阈值非法）；成功 → 丢弃实例（仅验证用，worker 各自构造）。
5. 构造 `DetectorRunner(config_path, max_workers=detector_workers, topk_n, tk2cat, vocab_size)`
   （含 `ProcessPoolExecutor`，initializer 注入 tk2cat，见 §4.4）。

`enabled=False` → 跳过全部 eager 初始化（无 detector/tokenizer/进程池/校验），纯透传模式；
指标端点仍可达报零值。

**`__call__(scope, receive, send)` 分派**：
1. 非 http scope → 透传 `self.app`。
2. `GET <metrics_path>` → 内联 `_serve_metrics`。
2.5. `path == config_path`：GET → `_serve_config`；POST → `_handle_config_update`；其他 → 透传。
3. 非 POST、非目标路径、或 `enabled=False` → 透传（不读 body）。
4. 异常监控概率：`will_detect = random.random() < _monitor_rate`；未选中 → 纯透传（不读 body、
   不注入、不恢复、不检测，见 §3.5）。
5. 选中：`_read_all_body(receive)` 聚合 body → `json.loads`；非 dict/非 JSON →
   `_make_replay_receive(receive, raw, request_id)` 原样重放透传。
6. `save_original_params` → `inject_params`（注入值 max(客户端,N)）→
   `_patch_scope_content_length(new_body_len)` → 建 `RequestContext`
   (orig/model/request_id/will_detect) →
   `replay_receive = _make_replay_receive(receive, new_body, request_id)` →
   装 `ResponseInterceptor`（透传 `self._resolver`，启动期已就绪）→
   `await self.app(new_scope, replay_receive, interceptor)`。

**`_make_replay_receive(original_receive, body, request_id)`**：首次调用返回合成
`{"type":"http.request","body":body,"more_body":False}`；**后续调用委托
`await original_receive()`**，绝不返回空 body 的 `http.request`（vLLM 会重复处理请求）。
透传（非 JSON/非 dict）与注入两条路径都经此包装——body 已被读走必须重放。

无 `_ensure_runner` / `_ensure_resolver` / `_start_preheat` 方法——启动期 eager 初始化
已完成全部重活（numpy、tokenizer、检测器构造、进程池建池），请求路径无懒加载、无双检锁、
无预热线程。runner/resolver 在请求路径直接引用 `self._runner` / `self._resolver`（必非 None，
因 `enabled=True` 时启动期已构造成功；`enabled=False` 时根本不走注入路径）。

**`_serve_metrics(send)`**：`render_metrics()` → 200 + 正确 content-type +
正确 content-length，作为完整 ASGI 响应发出。

### 4.2 ResponseInterceptor（响应拦截器）

**构造**：`(send, *, ctx, runner, metrics, pending_tasks, resolver)`。`resolver`
为进程级共享引用（`TokenTextResolver`，启动期已就绪，§3.10），透传至流式 `SSEStreamProcessor` 与
非流式 `_process_complete` 的 `strip_*_response`。`enabled=True` 时 resolver 恒非 None
（启动期硬依赖，加载失败即终止服务）。
状态：`_is_streaming`、`_start_msg`、`_body_buf`、`_sse`、`_finished`、`_detection_scheduled`、`_detection_results`。

**`__call__(message)` 分派**：
- `http.response.start` → `_on_start`：判 `content-type` 含 `text/event-stream`；
  注入 `x-anomaly-request-id`；流式建 `SSEStreamProcessor(is_chat, orig, top_logprobs, resolver)` 并立即 send(start)；
  非流式缓冲 `_start_msg`。
- `http.response.body` → `_on_body`。
- 其它 → 透传。

**`_on_body` 流式分支**：
```
out = _sse.feed(body)
if more_body:
    if out: send({type:"http.response.body", body:out, more_body:True})
else:
    if _finished: return          # 防重复
    tail = _sse.flush()
    send({type:"http.response.body", body:(out or b"")+tail, more_body:False})
    _finished = True
    _maybe_schedule_detection()
```

**`_on_body` 非流式分支**：
```
_body_buf.extend(body)
if more_body or _finished: return
final = _process_complete()       # extract+strip+reserialize；非 JSON 原样透传
_send_start(final)                # 注入关联头 + patch 响应 CL
send({type:"http.response.body", body:final, more_body:False})
_finished = True
_maybe_schedule_detection()
```

**`_process_complete`**：`json.loads(_body_buf)`；失败→返回原始 bytes（透传，不注入检测）；
成功→`extract_*_response` 得 per-choice `(logprobs, token_ids)` numpy 数组存 `_detection_results`，
再 `strip_*_response(data, orig, self._resolver)`（§3.10 resolver 优先还原文本），
`json.dumps(...).encode()` 返回。

**`_send_start(final)`**：从 `_start_msg` patch headers：改写/补 `content-length`，
注入 `x-anomaly-request-id`，send。

**`_maybe_schedule_detection`**：
```
if not will_detect or _detection_scheduled or _runner is None: return
_detection_scheduled = True
try: logprobs_list, token_ids_list = _get_detection_inputs()
except: log; return
if not token_ids_list or not any(len(t) > 0 for t in token_ids_list): return
schedule_detection(_runner, logprobs_list, token_ids_list,
                   request_id=_request_id, model=_model, metrics, pending_tasks)
```
`_get_detection_inputs`：非流式取 `_detection_results`；流式取 `_sse.get_detection_data()`。
两者返回 2-元组 `(logprobs_list, token_ids_list)`（per-choice numpy 数组列表；无
model_configs——model 仅用于指标标签）。`schedule_detection` 内部分配 SharedMemory、
写入两数组、构造元数据后提交检测（§4.4）。

### 4.3 SSEStreamProcessor

见 §3.3 状态机。要点：
- `feed(chunk)->bytes`：缓冲 + `\n\n` 切分 + `_process_event`。
- `flush()->bytes`：排空尾部。
- `get_detection_data()->(logprobs_all, token_ids_all)`（2-元组，per-choice numpy 数组列表）。
- 构造增 `resolver` 形参；`_strip_streaming` 调 `strip_*_response(parsed, orig, self._resolver)`
  透传 resolver（§3.10）；`_extract_streaming`（检测累积）**不变**——继续用 token_id 整数。
- 累积（有状态）与每块恢复（无状态）并存。

### 4.4 DetectorRunner

**构造** `(config_path, max_workers=4, topk_n=None, tk2cat=None, vocab_size=None)`：
`ProcessPoolExecutor` + 共享内存零拷贝。`max_workers` 来自 `PluginConfig.detector_workers`
（默认 4）。`topk_n` 为检测器 topk 截断参数（来自 `PluginConfig.top_logprobs`）。
建池时经 `initializer=_worker_init`、`initargs=(config_path, tk2cat, vocab_size, topk_n)` 注入
tk2cat 到每进程 worker（§3.11）。持 `_broken` 标志用于进程池崩溃恢复。

**模块级函数**（须可 pickle，Windows spawn / Linux fork 均可）：

```python
_worker_state = {}

def _worker_init(config_path: str, tk2cat, vocab_size: int, topk_n: int):
    """每进程初始化：构造检测器 + 注入词表。"""
    from .detector import ILLDetector
    det = ILLDetector(config_path)        # 启动期主进程已 eager 验证，此处必成功
    if tk2cat is not None:
        det.set_vocabulary(tk2cat, vocab_size)
    _worker_state["detector"] = det
    _worker_state["topk_n"] = topk_n

def _detect_sync(metadata: dict):
    """worker 检测入口：从共享内存零拷贝读取 → 逐候选检测 → 返回结果。"""
    from multiprocessing import shared_memory
    import numpy as np
    shm = shared_memory.SharedMemory(name=metadata["shm_name"])
    try:
        off = metadata["offsets"]; shapes = metadata["shapes"]
        # 零拷贝重建数组（不复制数据，直接映射共享内存）
        logprobs = np.ndarray(shapes["logprobs"], buffer=shm.buf,
                              offset=off["logprobs"], dtype=np.float32)
        token_ids = np.ndarray(shapes["token_ids"], buffer=shm.buf,
                               offset=off["token_ids"], dtype=np.int32)
        det = _worker_state["detector"]; topk_n = _worker_state["topk_n"]
        results = []
        for i in range(metadata["num_choices"]):
            n = metadata["choice_lengths"][i]
            res = det.detector(logprobs[i][:n], token_ids[i][:n], topk_n=topk_n)
            results.append([res.is_ill, res.ill_type])
        return results
    finally:
        shm.close()   # 关闭本进程映射（不 unlink，由主进程释放）
```

**`run_async(metadata: dict)`**：`loop.run_in_executor(self._executor, _detect_sync, metadata)`。
`metadata` 含 `shm_name`、`num_choices`、`topk_n`、`choice_lengths`、`shapes`、`offsets` 等
（见 §3.3 共享内存数据格式）。

**`set_vocabulary(tk2cat, vocab_size)`**：tk2cat 在启动期固定（§3.11），建池时经 initializer
注入每进程。运行期不再调用 `set_vocabulary`（如需更新则重建进程池，当前设计无此需求）。

**`_rebuild_pool()`**：捕获 `BrokenProcessPool`（worker segfault/OOM）时调用——重建
`ProcessPoolExecutor`（重新 `initializer` 注入 tk2cat），该请求计 error + log，后续请求
在新进程池上正常检测。

**`shutdown()`**：`self._executor.shutdown(wait=False, cancel_futures=True)`；清理所有
未释放的 SharedMemory 块。

**`schedule_detection(runner, logprobs_list, token_ids_list, *, request_id, model, metrics, pending_tasks) -> Task`**：
① 分配 SharedMemory + 写入两数组（logprobs, token_ids）+ 构造元数据；
② `asyncio.create_task(_run_detection(runner, metadata, ...))`；
③ `pending_tasks.add(task)`；`done_callback` → `pending_tasks.discard` + `_cleanup_shm`。
内部 `_run_detection()`：`detection_duration.time()` 计时 → `runner.run_async(metadata)` →
`record_detection(results, model)`；`except BrokenProcessPool` → `runner._rebuild_pool()` +
`record_error()` + log；`except Exception` → `record_error()` + log。最后 `_cleanup_shm(metadata)`。
`model` 仅用于指标标签，不参与检测。

### 4.5 检测器契约要点

> **vendored 含义**：`detector.py` 是检测算法源码被**直接内置进本项目包**
> （vendor 进项目），随中间件分发。`configs/detector.yaml` 是其算法默认参数。
> 配置路径固定见 §3.8（固定 `configs/detector.yaml`，缺失 → 启动期 raise）。

- 构造 `ILLDetector(config_path)`：仅加载 `detector.yaml`（算法阈值），
  无模型识别文件、无预生成映射文件依赖。启动期主进程 eager 构造验证（§4.1）；
  worker 进程构造必成功（主进程已排除环境问题）。
- **`set_vocabulary(tk2cat, vocab_size)`**：接受启动期生成的 `{str(token_id): category}`
  映射（§3.11）。幂等，重复调用覆盖。经 `ProcessPoolExecutor` initializer 在每进程构造
  检测器后同步注入（tk2cat 非 None 时）。`get_tk2cat()` 返回注入的映射或 `(None, None)`
  （未注入 → 无词表降级）。
- **`topk_n` 参数**：`run(logprobs, token_ids, topk_n=N)` 由参数传入 topk 截断值，
  消除实例态 `topk` 首次锁定问题（`top_logprobs` 仍须跨请求恒定以保语义一致，
  但不再因实例态复位缺陷强制）。
- **输入格式**（dict → numpy 数组，算法逻辑不变）：
  ```
  当前:  detector(topk_logprobs: List[Dict[int, float]], tokens: List[int], topk_n)
  改后:  detector(logprobs: np.ndarray, token_ids: np.ndarray, topk_n)
         logprobs:  shape=(num_tokens, topk_n), float32, 已降序排列
         token_ids: shape=(num_tokens, topk_n), int32, 与 logprobs 同序
         # tokens 不再单独传入，内部取 token_ids[:, 0] 作为输出 token 序列
  ```
- **批量入口**：`run(logprobs: list[np.ndarray], token_ids: list[np.ndarray],
  topk_n: int) -> list[[bool,int]]`。
- **逐方法数据访问变更**（逻辑不变，仅 dict→数组索引）：
  - `_sort_and_truncate_topk`：`dict(sorted(d.items(), key=...)[:topk])` →
    `np.argsort(-logprobs, axis=1, kind='stable')[:topk]` + `take_along_axis`
    （`kind='stable'` 保证 tie-breaking 与 Python `sorted()` 一致）。
  - `detector()` L397：`np.array([list(item.values()) for item in topk_logprobs])` 删除
    （已是数组）；新增 `tokens = token_ids[:, 0]`（top-1 作为输出 token 序列，用于
    n-gram/滑窗/轨迹，与原 `tokens` 语义一致）。
  - `_detect_rare_character` L295：`list(window_topk_logprobs[dim].keys())` →
    `window_token_ids[dim]`（数组索引，查的 token_id 相同）。
  - `_detect_garbled` L240：`len(window_topk_logprobs)` → `len(window_logprobs)`（同值）。
  - 主循环 L417：`topk_logprobs[start:end]` → 增加 `token_ids[start:end]` 切片；
    `window_tokens` 改取 `token_ids[start:end, 0]`。
- **实例态副作用**：`_garbled_count` 每请求复位。多进程隔离——每 worker 进程独立
  `ILLDetector` 实例，实例态天然隔离，无需锁。
- **无词表降级**：`tk2cat` 为 `None` 时，rare/garbled 走 top1 logp 路径
  （按概率阈值判异常），repetition/acf/trajectory 不受影响。
- **`schedule_detection`**（`detector_runner.py`）：分配 SharedMemory + 写入两数组
  （logprobs, token_ids）+ 构造元数据（`shm_name`/`num_choices`/`topk_n`/
  `choice_lengths`/`shapes`/`offsets`）→ `asyncio.create_task(_run_detection(...))`。
  变长候选按最长 padding，`choice_lengths` 记录实际长度，worker 按此切片。

### 4.6 Config / Metrics

- `env.py`：`PluginConfig`（env 读取+校验，含 `detector_workers` 默认 4）；`resolve_config_path()` 返回固定路径
  `configs/detector.yaml`（项目根目录），文件不存在 → raise（启动期硬依赖）。
- `metrics.py`：独立 registry；`record_detection(results, model)`、`record_error()`、
  `render_metrics()->bytes`、`METRICS_CONTENT_TYPE`。

### 4.7 TokenTextResolver

**职责**：token_id(int)→单 token surface 文本（`decode([id])`）；进程级单例，启动期同步构造，
失败 raise（启动期硬依赖）。仅被 strip 路径（事件循环）调用，检测侧用 token_id 整数、不调用
resolver（§3.10）。

**构造** `TokenTextResolver(tokenizer)`：持 tokenizer 引用与空 `dict[int,str]` 缓存。

**`resolve(token_id) -> Optional[str]`**：`int(token_id)` → 命中缓存直接返回；否则
`tokenizer.decode([tid])`，非空存入缓存并返回、空则缓存 `None` 并返回 `None`。`decode` 对个别 id
抛错由 try/except 吞为 `None`（该处置 null），不影响其余。

**`acquire_tokenizer(explicit) -> Any`**（同步，`token_resolver.py`）：tokenizer 获取经
env/argv/缓存自动发现，顺序见 §3.10（不含 HTTP loopback）。`_from_pretrained` 为间接层
（便于测试 monkeypatch）。`parse_vllm_argv(argv=None) -> Optional[VllmArgvInfo]` 解析
`vllm serve <model> ... --tokenizer <path> ... --host H --port P`，返回
`VllmArgvInfo(model, tokenizer, host, port)` 或 None（非 serve 命令）；
`parse_vllm_server_from_argv(argv=None) -> Optional[(host, port)]` 为向后兼容封装。
`_scan_hf_cache_candidates(hint)` 走 `huggingface_hub.scan_cache_dir()`，不可用返回 []。
均失败 → raise（终止服务启动）。

**并发**：`resolve` 仅事件循环单线程调用，`dict` 缓存无锁；`acquire_tokenizer` 仅在
`__init__` 启动期调用一次（§6.4）。无 loopback HTTP、无 `_ensure_resolver` 双检锁。

**降级**：resolver 为启动期硬依赖——加载失败 → raise 终止服务启动（不软降级）。
运行期仅个别 token 的 `decode([id])` 抛错 → 该 token 返回 `None`（置 null），不影响其余。
resolver 就绪后 strip 路径 `_token_text` 优先 resolver（§3.10），个别 decode 失败时退回
bytes 兜底或 null；不影响检测、不影响客户端响应完整性。

## 5. 数据流

### 5.1 chat 非流式
```
client POST /v1/chat/completions (无 logprobs)
  → 读 body, parse JSON; will_detect (say True)
  → save_original_params; inject(logprobs/top_logprobs/return_tokens_as_token_ids)
  → patch 请求 CL; 装 ResponseInterceptor(透传 self._resolver, 启动期已就绪)
   → app(...) 返回 choices[].logprobs.content[] (含 bytes, token="token_id:NNN")
   → _on_start(缓冲); _on_body(缓冲到 more_body=False)
        _process_complete: extract→存检测数据(logprobs,token_ids numpy 数组); strip(resolver)→logprobs=null, token_id→文本(resolver 优先, bytes 兜底)
        _send_start(注入关联头 + patch 响应 CL); send(terminal body)
   → 调度检测(logprobs_list, token_ids_list, request_id, model)
client 收到: logprobs=null, 无 token_id:, 带 x-anomaly-request-id
worker: _detect_sync→从 SharedMemory 零拷贝读取→ILLDetector.detector(logprobs, token_ids, topk_n)→[[is_ill,ill_type]]→record_detection
```

### 5.2 chat 流式
```
client POST ... (stream=true)
  → ... inject ... ResponseInterceptor
  → _on_start: content-type 含 text/event-stream → 建 SSEStreamProcessor
       注入关联头; send(start) 立即
  → _on_body(more_body=True, body=chunk1):
       _sse.feed → 增量 strip+转发; 同时 _extract_streaming append 累积
  → ... 多块 ...
  → _on_body(more_body=False): _sse.flush; send(terminal); _finished=True; 调度检测
client 收到: 增量恢复块 + data: [DONE] (原样透传) + 关联头
worker: _detect_sync→从 SharedMemory 零拷贝读取累积全部 token→ILLDetector.detector(logprobs, token_ids, topk_n) → record_detection
```

### 5.3 completions
注入 `logprobs=<N>` + `return_tokens_as_token_ids=true`；恢复时若客户端未请求
`return_tokens_as_token_ids` → `tokens[]` 经 `_token_text(t, None, resolver)` 还原（resolver 可用为
真实文本，个别 decode 失败为 null，**绝不留 `token_id:`**）；`top_logprobs` dict 截断到请求 N、重建为 `{文本:logprob}`
（个别 decode 失败时落 null）。

## 6. 并发与生命周期

### 6.1 启动期 eager 初始化

- **全部初始化在 `__init__` 同步完成**（`enabled=True` 时）：见 §4.1 构造流程——
  `PluginConfig.from_env`（env 校验）→ `resolve_config_path`（文件存在校验）→
  加载 tokenizer（同步）→ `generate_tk2cat`（软降级）→ eager 构造 `ILLDetector`（验证）→
  构造 `DetectorRunner`（含 `ProcessPoolExecutor` + initializer 注入 tk2cat）。
- **无懒加载**：移除原两段式懒加载（廉价阶段在首 `will_detect` 请求、重阶段在 worker 线程）——
  启动期已完成全部重活（numpy、tokenizer、检测器构造、进程池建池）。请求路径无 `_ensure_runner`、
  无 `_ensure_resolver`、无 `_get_detector`、无双检锁。
- **无预热线程**：移除 `_start_preheat`——tokenizer 在 `__init__` 同步加载，无需后台预热。
- **原则**：重活（numpy 导入、tokenizer 加载、检测器构造、进程池建池）集中在启动期一次性完成，
  请求路径仅做轻量数据转换（dict→numpy 数组、写 SharedMemory、submit 元数据）。
- **失败语义**：硬依赖失败 → raise 终止服务启动（fail-fast）；tk2cat 失败 → 软降级（无词表检测）。
- 启动期开销：numpy 导入 + tokenizer 加载 + tk2cat 生成 + 检测器构造 + 进程池建池（Windows spawn
  ~1-2s，Linux fork 更快）——一次性开销，进程生命期复用。

### 6.2 多进程并行检测

- 检测器 CPU-bound（numpy/FFT 占 40-50%，纯 Python 占 50-60%）；线程并行仅 numpy 部分释放
  GIL，有效加速约 1.5-2.5 倍（非 4 倍）。改用 `ProcessPoolExecutor` 实现真正物理并行，
  4 worker → 4 倍加速，应对高并发推理场景（几十至上百并发请求）。
- **每 worker 进程独立 `ILLDetector` 实例**：进程级隔离 `_garbled_count` 等实例态，
  无需锁、无并发竞争。
- **共享内存零拷贝数据传递**：主进程将检测数据预转换为 numpy 数组，写入
  `multiprocessing.shared_memory.SharedMemory` buffer，仅向 worker 提交 buffer 名称 +
  形状元数据（几十字节 pickle）。避免直接 pickle `List[List[Dict[int,float]]]` 阻塞事件循环
  （500 token ≈ 2-5ms pickle 开销）。
- **错误隔离**：单 worker 检测异常 → Future 返回异常 → `_run_detection` catch → log + 计 error，
  不影响其他 worker，不标记全局不可用，不重建进程池。`BrokenProcessPool`（worker segfault/OOM）
  → `_rebuild_pool` 重建 + 该请求计 error + log，后续请求在新进程池上正常检测。
- **配置**：`VLLM_ANOMALY_DETECTOR_WORKERS`（默认 4，建议范围 1-16，启动期校验 ≥1）。

### 6.3 检测任务生命周期

- fire-and-forget：`asyncio.create_task`，异常全捕获。
- 防 GC：`_pending_tasks` 持引用，`done_callback` 出集。
- 关闭：未完成任务随 loop 取消（结果丢失不影响客户端）。

### 6.4 resolver 生命周期

- resolver 在 `__init__` 启动期同步构造（`acquire_tokenizer` 完成后立即构造
  `TokenTextResolver`），无懒加载、无双检锁、无 `_ensure_resolver`。失败 → raise
  终止服务启动（启动期硬依赖）。
- 首次加载可能含一次 argv 解析 + 一次本地 tokenizer 加载（百毫秒级），一次性、进程生命期复用。
- `resolve` 仅事件循环调用；`dict` 缓存单线程访问，无锁。
- `shutdown` 无特殊清理（tokenizer 随进程退出）。

## 7. 边界条件与异常处理

| 场景 | 处理 |
|---|---|
| 非 JSON 请求体 | 透传，不注入 |
| 非 dict 请求体 | 透传，不注入 |
| 非 JSON 响应体（错误页） | `_process_complete` 失败→原样透传，不注入检测 |
| 空响应（无 token） | 不调度检测 |
| `configs/detector.yaml` 缺失 | 启动期 raise 终止服务启动，错误信息提示文件路径 |
| env 变量越界（top_logprobs/monitor_rate/detector_workers） | 启动期 raise 终止服务启动，错误信息提示范围 |
| tokenizer 加载失败（env/argv/HF 缓存均失败） | 启动期 raise 终止服务启动，错误信息提示设置 `VLLM_ANOMALY_TOKENIZER_MODEL` |
| numpy 未安装 / config 解析失败 | 启动期 eager 构造 ILLDetector 时 raise 终止服务启动 |
| 检测器抛异常 | 计 detection_error，不影响客户端（响应已发完）——推理期降级 |
| 进程池崩溃（BrokenProcessPool） | 重建进程池 + 该请求计 error + log，后续请求正常检测 |
| 下游多发终端 body | `_finished` 守卫，忽略后续，不重复调度 |
| 流式无 `[DONE]` 即断 | `flush` 排空残余，按已累积数据检测 |
| CRLF SSE | 行尾 `rstrip(b"\r")` 兼容 |
| metrics 路径被 app 路由占用 | 默认 `/anomaly/metrics` 避开 vLLM `/metrics`；可配置 |
| `Expect: 100-continue` | ASGI 不暴露，由 uvicorn/vLLM 处理，安全 |
| chunked 请求 | ASGI 已合并为完整 body，`_read_all_body` 正确 |
| 下游二次读 receive | 重放 receive 委托原始 `receive()` 取真实后续消息（`http.disconnect`）；不得合成空 body 的 `http.request`（实测 vLLM 会重复请求） |
| `VLLM_ANOMALY_TOKENIZER_MODEL` 设但 `from_pretrained` 抛错 | 落到 argv `--tokenizer`/`--model` 自动解析；记 INFO |
| argv `--tokenizer` 解析成功但 `from_pretrained` 抛错 | 落到 argv `--model` → HF 缓存扫描兜底；记 INFO |
| argv 无 `serve`（非 vLLM 命令 / 测试环境） | `parse_vllm_argv()` 返回 None，跳过 argv 路径，走 HF 缓存扫描 |
| argv / env / HF 缓存均无可用路径 | raise 终止服务启动（启动期硬依赖） |
| 缓存扫描命中但 `from_pretrained` 抛错 | 记 WARNING，继续下一候选；均失败 → raise 终止服务启动 |
| `decode([id])` 对个别 id 抛错 | `resolve` 内 try/except → 该 id 返回 None（置 null），不影响其余 |
| tk2cat 生成失败 | 软降级——服务正常启动，检测降级为无词表（rare/garbled 走 top1 logp），记 WARNING |
| tokenizer 无 decode 路径 | `generate_tk2cat` raise → tk2cat 不注入 → 无词表检测 |
| 自定义 `--tokenizer` 路径 | argv `--tokenizer` 优先于 `--model`，自动对齐 vLLM 实际 tokenizer；无需设 env |

## 8. 部署

项目路径：path=xxx/accuracy-monitoring/

安装：
```shell
# 进入项目路径
cd $path
# 安装包
pip install -e .
```


启动：`vllm serve <model> --middleware anomaly_middleware.AnomalyMiddleware`。

## 9. PD 分离部署设计（proxy 集成）

> 本章为 PD 分离（Prefill/Decode Disaggregation）场景的设计结论与实现要点；完整论证
> （首 token 归属源码证据链、两版 proxy 脚本差异分析、方案选型对比）见 `design_pd_proxy.md`。
> 单机模式（§1-§8）不受本章影响；对应功能规格见 `spec.md` §6。

### 9.1 背景与设计动机

PD 分离下，P 节点（`kv_role=kv_producer`）与 D 节点（`kv_role=kv_consumer`）以基本相同的
命令拉起 `vllm serve`。若沿用单机 `--middleware` 方案存在以下问题：

- **检测点混乱**：P 节点只做 prefill（请求被 proxy 改写为 `max_tokens=1`，响应被 proxy
  内部消费、不面向客户端），在其上检测无意义且会产生误报（单 token 易触发
  nan/repetition 误判）；输出 token 序列 100% 产生于 D 节点。
- **结果多份**：每个 D 节点各挂一份 middleware → 各自独立的 metrics 端点、各自的 pkl
  落盘（异常编号各自从 1 开始、互相冲突），webui 需聚合多源数据。
- **采样与动态配置全局失效**：`monitor_rate` 在各 D 节点独立采样，不同节点 env 不一致时
  全局行为不可预期；`POST /anomaly/config` 一次只能改一个节点。
- **资源浪费**：各 D 节点重复加载 tokenizer、构建检测进程池、生成 tk2cat 词表映射，
  挤占推理节点资源。
- **误配风险**：多 P 多 D 时漏配/多配 `--middleware` 均无告警，配置漂移难以审计。

**设计决策：检测能力上移到 proxy**，作为全局唯一检测点——一份结果、一个 metrics 端点、
一个 pkl 文件、一次动态配置全局生效；P/D 节点零侵入（启动命令回归纯净）；检测语义与
单机模式完全一致。

### 9.2 总体架构与数据流

proxy 是所有客户端请求的唯一入口与响应流唯一出口：

- **请求侧**：在转发前完成采样与参数注入（dict 级修改，无需单机版的 receive 重放与
  Content-Length 修补，框架会重新序列化）；
- **响应侧**：在转发前完成 strip 恢复与检测数据累积，流结束后 fire-and-forget 调度检测；
- **P 节点不参与检测**：其响应仅用于 KV 传输协商、被 proxy 内部消费；
- **检测数据抽取自 D 节点输出流**：D 重新生成首 token，其输出流天然完整（含首 token 及
  其 top-k logprobs），检测无盲区、无需 P/D 结果聚合（源码证据链见
  `design_pd_proxy.md` §3.1.1）。

部署形态图（P/D 节点命令纯净，proxy 为唯一检测点）与逐请求数据流（采样 → 注入 → D 流式
响应重组/恢复/累积 → 流结束调度检测 → Metrics/AnomalyStore）见 `design_pd_proxy.md`
§4.1/§4.2。

### 9.3 组件设计与实现要点

**新增模块 `proxy_integration.py`**（纯新增，不改动既有模块）：

| 组件 | 职责 |
|---|---|
| `ProxyAnomalyMiddleware` | PD 模式 ASGI 中间件：继承单机版 `AnomalyMiddleware`，完整复用其 `__call__`（端点/采样/ASGI 注入/恢复/调度）；仅替换初始化——无 tokenizer，启动期加载预生成 tk2cat + 文本表文件（fail-fast） |
| `ProxyAnomalyController` | handler 挂钩控制器（进程内单例）：`before_forward`（采样+快照+注入）→ `feed_stream`（SSE 重组+恢复+累积+事件上抛，内部异常兜底为原样透传）→ `on_recompute`（重置累积）→ `finish`（收尾+调度检测） |
| `ProxyStreamProcessor` | 流式 SSE 重组 + strip 恢复 + 检测数据累积 + 解析事件上抛（复用 `SSEStreamProcessor`）；非流式字节缓冲 + 整体 extract/strip |
| `StaticTokenTextResolver` | 预生成 token 文本表查表：`resolve(token_id) -> Optional[str]` 与 `TokenTextResolver` 同接口（duck-typing），strip 调用侧零改动；文本为空 → None → 既有 bytes/null 兜底 |

**新增工具**：

- `tools/gen_token_category.py`：离线词表映射生成工具（独立可执行，仅依赖 `transformers`，
  分类逻辑与 `token_categorizer.py` 逐行一致，保证类别体系与运行期检测算法相同）；
- `tools/run_proxy_with_anomaly.py`：独立启动器（零修改运行任意版本 proxy 脚本）。

**词表类别映射与文本还原：离线预生成文件**（替代单机版运行期 tokenizer 加载——
proxy 服务器通常无模型文件）：

1. 在有模型/Tokenizer 的服务器执行 `gen_token_category.py --model-path <模型目录>`，
   一次遍历产出**同名成对**两文件：`token2category/<model_name>_<vocab_size>.json`
   （`{str(token_id): category}`）与 `token_text/<model_name>_<vocab_size>.json`
   （`{str(token_id): surface_text}`）；文件名仅人工识别用，`vocab_size` 由映射内容推断
   （`max(键) + 1`）；
2. proxy 侧经 `VLLM_ANOMALY_TOKEN2CATEGORY` 指定具体映射文件，启动期一次性 eager 加载
   （无懒加载、无运行期查找）；文本表默认取映射文件**兄弟目录 `token_text/` 下同名文件**
   （`derive_token_text_path`），可用 `VLLM_ANOMALY_TOKEN_TEXT` 覆盖；
3. tk2cat 经 `DetectorRunner` 建池 initializer 注入 worker——注入路径与单机版完全一致；
4. fail-fast：文件缺失/非法（非 JSON 对象、映射为空）→ proxy 启动终止（显式配置错误
   不静默降级）；文件内个别 token 键缺失 → 按检测器无词表降级规则处理（同 §3.11）。

**集成模式**（三选一，互斥——叠加会双重采样/注入）：

| 模式 | 方式 | proxy 脚本改动 | 适用场景 |
|---|---|---|---|
| A. handler 挂钩 | `ProxyAnomalyController` 挂入 `_handle_completions`/`generate_stream`，事件消费与 recompute 感知最精细 | 多处 | 深度定制/调试 |
| B. 一行包裹 | `app = build_proxy_middleware(app)`（未配置词表时原样返回 app，零行为差异） | 1 行 | 版本升级后快速重挂 |
| C. 独立启动器 | `tools/run_proxy_with_anomaly.py <proxy脚本> [proxy原参数] [--anomaly-*...]`：按路径导入 proxy 模块 → 包裹 app → uvicorn 启动；`--anomaly-*` 自动映射为 `VLLM_ANOMALY_*` 环境变量 | 0 行 | 不改上游脚本，随版本直接替换 |

- 模式 B/C 的可行性依据：proxy 的 recompute 为**续写语义**（重发请求 prompt 含已生成
  token），客户端可见流本身即完整序列——ASGI 中间件无感知 recompute，SSE 累积天然覆盖
  全部 token（含首 token），无需重置逻辑；
- 启动器检测到 proxy 脚本已内置挂钩（`_init_anomaly`）→ 拒绝启动并提示，防止双重集成；
- 未配置词表且未显式禁用 → 按未启用检测运行（proxy 行为与原版逐字节一致）。

**recompute 一致性**（模式 A）：D 节点返回 `stop_reason=="recomputed"` 触发 proxy 重构
请求重发时，上一轮流是不完整推理——`on_recompute` 重置累积器与重组缓冲（`orig/model/
prompt` 保留），已积累的不完整序列不送检测；`finish` 仅在生成器最终退出时调用一次，
检测数据以**最终成功流**为准（避免重复计数/误检）。

**全局采样与动态配置**：采样在 proxy 单点执行（规则同 §3.5）；请求只命中一个 D 节点，
请求级检测概率**全局精确等于** `monitor_rate`；`POST /anomaly/config` 一次调用全局生效
（复用单机版端点实现）。通用版 proxy 多 uvicorn worker 时，动态配置需经既有
`SchedulerManager` 通道广播到各 worker（layerwise 版单进程无此问题，见
`design_pd_proxy.md` §5.5）。

**指标与落盘单点化**：复用 `metrics.py` / `anomaly_store.py` 不变——一个
`CollectorRegistry`、一份 pkl（异常编号全局唯一连续、重启续接）；`model` 标签 /
`model_name` 取请求体 `model` 字段（缺失 `"unknown"`），与单机版一致；webui 无需
聚合改造。

**降级**（对齐 §3.9）：启动期硬依赖（env/CLI 校验、`configs/detector.yaml`、词表/文本表
文件、ILLDetector 构造）失败 → proxy 启动终止；推理期检测异常/进程池崩溃 → log + 计
error + 不影响客户端与后续请求；检测层全部异常与转发主路径隔离——`feed_stream`/`finish`
内部异常兜底为**原样透传字节**并记日志。

**配置项**：沿用全部 `VLLM_ANOMALY_*` 环境变量（语义不变），新增
`VLLM_ANOMALY_TOKEN2CATEGORY`（PD 模式启用检测必填）与 `VLLM_ANOMALY_TOKEN_TEXT`
（可选覆盖文本表路径）；`VLLM_ANOMALY_TOKENIZER_MODEL` 在 PD 模式下不再使用。
完整 CLI 参数见 `design_pd_proxy.md` §7。

### 9.4 与单机模式的差异对照

| 维度 | 单机（--middleware） | PD（proxy 集成） |
|---|---|---|
| 部署位置 | 每个 vllm serve 进程 | 仅 proxy（1 个） |
| 拦截机制 | ASGI 中间件（receive 重放 + CL patch） | dict 级修改 / ASGI 包裹 |
| 采样语义 | 每实例独立 | 全局精确单点 |
| 动态配置 | 每实例分别 POST | 一次 POST 全局生效 |
| 结果/指标/pkl | 每实例一份（冲突风险） | 全局唯一一份 |
| strip 恢复位置 | vllm 进程内 | proxy 转发路径 |
| recompute | 无此场景 | 模式 A 重置累积器；模式 B/C 续写语义天然覆盖 |
| tokenizer | env/argv/HF 缓存自动发现 | 不加载：预生成 token2category/token_text 文件 |
| 文本还原 | `TokenTextResolver`（tokenizer.decode） | `StaticTokenTextResolver`（预生成文本表查表，同接口） |
| 首 token 检测 | 全序列由本实例生成 | D 重新生成首 token，D 流天然完整，无需聚合 |
| P/D 节点改动 | 需加 `--middleware`（易错） | 零改动 |


