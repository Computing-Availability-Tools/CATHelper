# 推理精度异常检测中间件 技术规格说明书(SPEC)

## 1. 目的与范围

本文档规定基于 vllm 的在线精度异常检测中间件 `anomaly_middleware` 的功能行为、输入输出契约与验收标准。
中间件通过 vLLM 的 `--middleware` 插件部署，监控推理异常现象，具体功能包括：拦截推理请求、
强制采集 logprobs 和 token_id、后台运行算法异常检测、不影响客户端请求响应状态返回、
并通过独立 Prometheus 端点暴露检测结果。整个过程客户端无感知。

适用范围：vLLM 的 `/v1/chat/completions` 与 `/v1/completions` 在线推理请求端点，
流式与非流式在线推理请求均覆盖。

## 2. 功能需求

### 2.1 请求拦截

中间件仅拦截 `/v1/chat/completions` 与 `/v1/completions`。
所有其他 HTTP 请求（任意方法或路径）均原样转发给下游应用（保持原先vllm处理方式一致）。

**验收**
- `GET /v1/models` → 原样转发。
- `GET /v1/chat/completions` → 原样转发。

### 2.2 强制 logprobs、token_id 采集

- 对每个被拦截请求，缓存客户端原始 logprobs、token_id 等相关采集参数，供响应恢复；

- 无视客户端请求原值，对请求体强制注入检测所需参数：chat 设 `logprobs=true`、
`top_logprobs=<N>`、`return_tokens_as_token_ids=true`；completions 设 `logprobs=<N>`、
`return_tokens_as_token_ids=true`，其中 N 默认设置为20。请求 `Content-Length` 修正为新 body 长度。


**验收**
- chat 请求下，客户端未带 logprobs 的  → 转发 body 含 `logprobs=true`、`top_logprobs=<N>` 和
  `return_tokens_as_token_ids=true`，`Content-Length` 反映新长度。
- chat 请求下，客户端配置 `logprobs=True` 和 `top_logprobs=5` 且配置 N=20 → 转发 `top_logprobs=20`，原 5 被内部保留用于恢复。
- chat 请求下，客户端配置 `logprobs=True` 和 `top_logprobs=10` 且配置 N=5 → 转发 `top_logprobs=10`，原 10 被内部保留用于恢复。
- completions 请求下，客户端未带 logprobs 的  → 转发 body 含 `logprobs=<N>` 和
  `return_tokens_as_token_ids=true`，`Content-Length` 反映新长度。
- completions 请求下，客户端配置 `logprobs=5`且配置 N=20 → 转发 `logprobs=20`，原 5 被内部保留用于恢复。
- completions 请求下，客户端配置 `logprobs=10`且配置 N=5 → 转发 `logprobs=10`，原 10 被内部保留用于恢复。
- chat 和 completions 请求下，客户端未带`return_tokens_as_token_ids=True`的 → 转发 body 含`return_tokens_as_token_ids=True`，原默认参数`return_tokens_as_token_ids=False` 被内部保留用于恢复。
- chat 和 completions 请求下，客户端请求设置 `n=4` → `n=4`参数被内部保留用于恢复。

### 2.3 客户端透明响应恢复

响应处理后恢复为客户端原始请求被满足时的形态：
- 客户端未请求 `logprobs`/`top_logprobs`  → `choice.logprobs` 置 null(vllm默认关闭)。
- 客户端请求 `logprobs=M`/`top_logprobs=M` → 各 top-logprobs 列表截断至 M(vllm默认关闭)。
- 客户端未请求 `return_tokens_as_token_ids=True` → 恢复响应中默认不得出现任何 `token_id:` 前缀字符串；
  token 文本经 `TokenTextResolver`（§2.15）以 `decode([id])` 还原，resolver 优先、`bytes` 兜底（仅当解码出真实
  文本且不含 `token_id:` 前缀）、无法还原时按 §4.7 例外分流（触发降级 → `token_id:NNN`，未触发 → null）。
- 适用于 chat 与 completions，流式与非流式。

**验收**
- chat 客户端未请求 采集 logprobs 和 token_id → 恢复后 `choice.logprobs=null`，全文无 `token_id:`。
- chat 客户端设置`logprobs=true`、`top_logprobs=3` → 截断 `top_logprobs`，取前 3 项数据，
  每项 `token` 为解码文本（非 `token_id:`）。
- chat 客户端设置`logprobs=true`、`top_logprobs=3`、真实 vLLM 响应（top bytes 为 `token_id:` 形态）
  → 每项 `token` 为 resolver 还原的真实文本，全文无 `token_id:`；单 token decode 失败且
  触发降级例外（客户端请求了 topk + 未设 return_tokens_as_token_ids）→ 该 token 回退 `token_id:NNN`（保证 topk logprob
  数据不丢失），主 token 仍优先 bytes 真实文本（三层兜底）。
- chat 客户端设置`logprobs=true`、`top_logprobs=3`和`return_tokens_as_token_ids=True` →  截断 `top_logprobs`，取前 3 项数据，且每项原样保留 `token_id:`。
- completions 客户端未请求 采集 logprobs 和 token_id → 恢复后 `choice.logprobs=null`，全文无 `token_id:`。
- completions 客户端设置`logprobs=3` → 截断 `top_logprobs`，取前 3 项数据,
  每项 `token` 为解码文本（非 `token_id:`）；单 token decode 失败且触发降级例外 → 该 `tokens[]` 项与
  `top_logprobs[]` 键回退 `token_id:NNN`（保证 topk logprob 数据不丢失）；未触发例外时 `tokens[]` 项为 null、`top_logprobs[]` 为 null。
- completions 客户端设置`logprobs=3`、`return_tokens_as_token_ids=True` →  截断 `top_logprobs`，取前 3 项数据，且每项原样保留 `token_id:`。
- chat 和 completions 客户端设置`logprobs=10`，而推理服务环境变量设置top-logprobs 数 N=4  → body 内 top-logprobs 的数量取二者最大值 10，推理请求输出的每个 token 有 10 项数据，送至检测截断前 4 项数据，返回给客户端 10 项数据。
- chat 和 completions 请求下，客户端请求设置 `n=4` → 循环处理 4 份候选结果，客户端输出格式按上述方法验收 


### 2.4 流式安全转发

对 `text/event-stream` 响应，保持流式响应原始机制，对 chunk 结果进行处理，缓存 logprobs 和 token_id 数据，转换为用户原始响应格式逐事件增量转发，不缓冲整流。终端 `data: [DONE]` 保留。
跨 body 块的半事件先重组再处理。


**验收**
- 多块 SSE 流 → 客户端随处理增量收到恢复块 + 终端 `data: [DONE]`，中间件不先全缓冲。
- 一条 SSE 事件被拆到两块 → 重组后处理，客户端收到一条完整事件。
- 流式响应中，处理 chunk 数据 → 中间件缓存整条 logprobs 和 token_id 数据用于检测，客户端输出格式参考2.3章节验收

### 2.5 异常检测执行

对每个被选中检测的请求：
- 针对单请求单输出，对输出 choice 抽取 top-k logprobs 与 token_ids（numpy 数组，
  已降序排列），提交检测后端；`tokens` 不再单独提取，检测器内部取 `token_ids[:, 0]`
  （top-1 after stable sort）作为输出 token 序列用于 n-gram/滑窗/轨迹检测；

- 针对单请求多候选输出的情况，循环抽取输出 choice 中的 top-k logprobs 与 token_ids
  （numpy 数组），数据用列表存储，送入检测，异常检测结果都应该上报，不能被覆盖。

检测结果按 `ill_type` 分类：
0=normal,1=rare_character,2=garbled,3=repetition,4=nan_value。
检测仅在响应全部发送给客户端后调度（fire-and-forget），客户端不等待检测。


**验收**
- 非流式请求 2 个请求被选中 → 每个推理结果 choice 抽取 `(logprobs, token_ids)` 提交检测，
  返回每 choice 一个 `[is_ill, ill_type]`。
- 空响应（无生成 token，如错误或空 completion）→ 不提交检测，情况记录至日志。
- 流式响应结束后，用跨块缓存的 logprobs 和 token_id 数据提交检测，客户端不等检测。
- 客户端请求中设置 `n=3`（vllm 默认 n=1）→ 单独处理该请求，将 3 份数据提交检测，若有多份数据检出异常，分别上报，不能覆盖


### 2.6 检测失败隔离

抽取、调度或检测期间任何异常须被捕获并记录，不得改变客户端响应、状态码或头部
（响应在检测时已发完）。检测失败计为 detection-error 指标。

**验收**
- 检测器运行中抛异常 → 客户端响应不受影响（已发完），计 detection-error，详细情况记录日志，
  后续请求正常处理。

### 2.7 多进程并行检测

多请求下检测调用经 `ProcessPoolExecutor` 多进程并行执行，工作进程数由环境变量
`VLLM_ANOMALY_DETECTOR_WORKERS` 配置（默认 4，须 ≥1）。每个工作进程持有独立的
`ILLDetector` 实例（进程隔离 `_garbled_count` 等内部状态）。检测数据经 `SharedMemory`
零拷贝传递：主进程将 numpy 数组（logprobs + token_ids）写入共享缓冲区，仅向工作进程
提交 buffer 名称 + 形状元数据（几十字节 pickle）；工作进程通过 `np.ndarray(buffer=shm.buf)`
零拷贝读取后逐候选检测。

单请求多候选输出下，各候选在工作进程内独立检测，异常检测结果都上报，不能被覆盖。

**验收**
- 两个被选中请求同时完成 → 两路检测并行执行（两个工作进程同时工作），而非串行。
- 单请求多候选输出 → 各候选数据分别送入检测，结果独立上报。
- `VLLM_ANOMALY_DETECTOR_WORKERS=8` → 8 路并行检测。
- 检测数据经共享内存零拷贝传递，事件循环不被阻塞（检测 100 并发请求时事件循环无显著延迟）。
- 某工作进程检测抛异常 → 该请求计 error + log，其他工作进程检测不受影响。
- 进程池崩溃（`BrokenProcessPool`）→ 重建进程池 + 该请求计 error + log，后续请求正常检测。

### 2.8 异常监控概率

支持可配置的异常监控概率 ∈[0.0,1.0]（`VLLM_ANOMALY_MONITOR_RATE`）。请求以等于该值的概率被选中做异常监控，未选中请求直接透传，不对用户请求做处理。1.0 表示全监控，0.0 表示不监控。
默认 1.0。

**验收**
- 监控概率 0.0 → 请求直接透传。
- 监控概率 1.0 → 所有请求都做异常监控。
- 监控概率 0.3  → 请求有 0.3 的概率会被修改请求内容，注入采集参数。有 0.7 的概率直接透传。
- 运行时可通过 `POST /anomaly/config` 动态更新监控概率（见 §2.17）。
  更新即时生效（下一请求即用新值），不持久化（重启回退 env 初始值）。

### 2.9 请求关联标识

为每个被拦截请求生成唯一关联标识，经 `x-anomaly-request-id` 响应头返回给客户端。
同一标识关联该请求的检测结果以供追踪。

**验收**
- 任意被拦截请求 → 响应含 `x-anomaly-request-id` 头，值为唯一。

### 2.10 Prometheus 指标暴露

在可配置 HTTP 路径（默认 `/anomaly/metrics`）响应 `GET`，内联作答不涉及下游路由。
指标使用与应用默认 registry 隔离的 registry。至少包含：已处理请求计数；
按 `ill_type` 与 `model` 标签的检测结果计数；检测错误计数；检测耗时直方图；
按 `ill_type` 与 `model` 标签的最近结果 gauge。

**验收**
- `GET /anomaly/metrics` → HTTP 200，`Content-Type: text/plain; version=0.0.4; charset=utf-8`，
  body 为 Prometheus 文本暴露。
- 下游无 `/anomaly/metrics` 路由 → 中间件上报不会报错，详细情况记录日志。
- `vllm_anomaly_monitor_rate` Gauge：当前异常监控概率（运行时可更新）。

### 2.11 环境变量配置

全部运行时配置从环境变量读取，如果没有配置，使用默认值。因构造除 app 外无参数。可选配：
总开关；异常监控概率；top-logprobs 数；检测工作进程数（§2.7）；指标端点路径；显式 tokenizer 加载源（§2.15）。

**验收**
- 总开关设置为 False → 不注入不检测（纯透传），但指标端点仍可达报零值计数。
- `VLLM_ANOMALY_TOKENIZER_MODEL=<vllm serve --model 实际值或 --tokenizer 值>` → 优先用其加载 tokenizer,
  覆盖 served 名为裸 basename / 本地目录部署。
- 未设 `VLLM_ANOMALY_TOKENIZER_MODEL` → 自动从同进程 `sys.argv` 解析 `vllm serve` 命令行
  （`--tokenizer` → `--model` 位置参数），无需用户额外配置。
- `VLLM_ANOMALY_DETECTOR_WORKERS` 非正整数 → 服务启动失败并报错。
- `VLLM_ANOMALY_CONFIG_PATH`（默认 `/anomaly/config`）：动态配置端点路径。


### 2.12 检测器配置路径

检测后端算法默认参数文件路径固定为 `configs/detector.yaml`（项目根目录），不可通过 env 覆盖。
启动期 `resolve_config_path()` 检查文件存在性，文件不存在 → 报错终止启动（fail-fast），
提示文件路径，不进入降级模式。

**验收**
- `configs/detector.yaml` 存在 → 使用该路径。
- `configs/detector.yaml` 缺失 → vllm serve 启动失败并报错，错误信息提示文件路径。

### 2.13 优雅降级——检测功能不可用

降级范围限定为推理期（检测执行、单 token decode、无运行事件循环、进程池崩溃）。
启动期硬依赖（`configs/detector.yaml`、env 变量、tokenizer 加载、ILLDetector 构造）
失败 → 报错终止启动（fail-fast），不进入降级模式。

推理期降级不改变客户端响应：检测执行异常、单 token decode 报错、进程池崩溃 → log +
计 error + 不降级算法 + 不影响其他请求/进程 + 后续请求继续检测。

`tk2cat` 生成失败为软降级（启动期）：无词表检测模式（算法内置 `get_tk2cat()→(None,None)`
降级路径），rare/garbled 走 top1 logprob，repetition/trajectory 不受影响，记 WARNING。

指标端点独立可达报零值。降级绝不改变客户端响应。

**验收**
- 启动期 `configs/detector.yaml` 缺失 / env 变量非法 / tokenizer 加载失败 / ILLDetector 构造失败
  → vllm serve 启动失败并报错，不进入降级模式。
- tk2cat 生成失败 → 服务正常启动（无词表检测模式，记 WARNING）。
- 推理期单请求检测执行异常 → 计 detection-error + log，客户端响应不受影响（已发完），
  后续请求正常检测，不设 `enabled=False`。
- 推理期单 token `decode([id])` 报错 → 该 token 返回 None + log，不终止推理，其他 token 正常还原。
- 推理期进程池崩溃（`BrokenProcessPool`）→ 重建进程池 + 该请求计 error + log，后续请求正常检测。

### 2.14 middleware 插件部署

仅加 `--middleware <module.path>.<ClassName>` 即可部署。构造函数仅接受一个参数
（被包装的 ASGI app）且无 kwargs，其中 ASGI 为 Asynchronous Server Gateway Interface。部署不要求 entry-point 注册、插件白名单或特定
vLLM 插件接口——仅需 vLLM 支持 `--middleware`。

**验收**
- `--middleware anomaly_middleware.AnomalyMiddleware` → 中间件以
  `AnomalyMiddleware(app)` 实例化，进程生命期内拦截目标请求。

### 2.15 token 文本还原（TokenTextResolver）

引入 `TokenTextResolver` 把强制注入产生的 `"token_id:NNN"` 还原为 token 文本回客户端（仅面向客户端
strip 路径，检测侧抽取继续用 token_id 整数、不动）。`vllm serve <model>` 进程内单一 tokenizer，
故 resolver 为进程级单例、一次加载、全请求复用。

**接口**：`resolve(token_id: int) -> Optional[str]`——返回该 token 的 surface 文本（`decode([id])`），
不可用返回 `None`（调用方置 null）。

**tokenizer 获取顺序**（启动期同步加载，`AnomalyMiddleware.__init__` 中执行）：
1. 显式 env `VLLM_ANOMALY_TOKENIZER_MODEL`（最高优先，设为 `vllm serve --model` 实际值或
   `--tokenizer` 的值）→ `from_pretrained(explicit, local_files_only=True)`。未设则跳过。
2. `--tokenizer` 从 `sys.argv` 解析（`parse_vllm_argv()`）：vLLM 启动命令中的 `--tokenizer <path>`
   即 vLLM 实际使用的 tokenizer 路径。非 `serve` 命令返回 None。
3. `--model` 位置参数从 `sys.argv` 解析：无 `--tokenizer` 时，`vllm serve <model>` 的
   `<model>` 即 tokenizer 路径。
4. HF 缓存扫描：以 argv model 名为 hint，served/model 名为裸 basename 而 HF 缓存键为完整 repo id 时，
   `huggingface_hub.scan_cache_dir()` 补全完整 repo id（短优先）后重试 `from_pretrained`。
5. 均失败 → 报错终止启动（fail-fast），错误信息提示设置 `VLLM_ANOMALY_TOKENIZER_MODEL`。

**恢复统一规则**（`_token_text(token_id, bytes, resolver, *, fallback_to_id=False)`）：resolver 优先
`decode([id])`；resolver 缺失/未解析 → 退回 `bytes`（仅当解码出真实文本且不含 `token_id:` 前缀）；都无 →
`fallback_to_id=True` 时回退 `token_id:NNN`（§4.7 降级例外），否则 null。completions 无 `bytes`，
单 token decode 失败时按触发条件命中与否分流：触发 → `token_id:NNN`，未触发 → null。

**降级**：tokenizer 在启动期同步加载（§2.15），加载失败即终止启动（不软降级），故运行期
resolver 恒可用。单 token `decode([id])` 报错时 `resolve()` 返回 None（逐 token，不终止推理），
该 token 文本按 §4.7 例外分流：触发降级例外（客户端请求 topk + 未设 return_tokens_as_token_ids）时回退
`token_id:NNN`（保证 topk logprob 数据不丢失，chat 主 token 仍优先 bytes 兜底真实文本——三层兜底），
未触发时回退 null/bytes，**绝不泄漏 `token_id:`**，不影响客户端、不影响检测（检测用 token_id 整数）。

**验收**
- chat 客户端设 `logprobs=true`、`top_logprobs=3`、未设 `return_tokens_as_token_ids`、真实 vLLM 响应
  （top bytes 为 `token_id:` 形态）→ 每项 `token` 为真实文本，全文无 `token_id:`。
- completions 客户端设 `logprobs=3`、未设 `return_tokens_as_token_ids` → `tokens[]`
  为真实文本、`top_logprobs[]` 为 `{文本:logprob}`（截断到 3），全文无 `token_id:`。
- 流式上述两路径逐块还原文本、不缓冲整流、`[DONE]` 透传、检测用 token_id 整数。
- 单 token decode 失败（`resolve()` 返回 None）+ 客户端请求了 topk + 未设 return_tokens_as_token_ids
  → 触发 §4.7 降级例外：chat 主 token 仍为文本（bytes 兜底，三层第二层；bytes 破碎时落
  `token_id:NNN`，三层第三层）、chat top_logprobs token 回退 `token_id:NNN`、completions
  `tokens[]`/`top_logprobs[]` 回退 `token_id:NNN`（保证 topk logprob 数据不丢失）；未触发例外
  （客户端未请求 topk）→ 维持 null/bytes、全文无 `token_id:`；检测正常。
- 客户端设 `return_tokens_as_token_ids=True` → 原样保留 `token_id:NNN`（不变）。
- 多候选 `n>1` → 每 choice 同上。

### 2.16 异常信息本地保存

当某被检测请求的某候选检出异常（`is_ill and ill_type != 0`）时，把异常现场保存为本地
pickle 文件（dict，key=异常编号，value=异常信息）。环境变量 `VLLM_ANOMALY_SAVE_PATH`
控制是否落盘。

- 异常信息字段：`time`（检出时间 Unix 秒）、`prompt`（请求 prompt 字段：chat 取 `messages`、
  completions 取 `prompt`）、`ill_type`（检出类型，数值型）、`topk_logprobs`（topk 信息，列表）、
  `tokens_ids`（输出 token_id 数据，列表）、`text`（该候选输出文本）、`model_name`（模型名）。
- 保存粒度：按异常候选，每个异常候选独立编号、独立记录；正常候选不保存。
- 路径模式：值以 `.pkl` 结尾→文件模式（路径即文件名）；否则→文件夹模式（文件名=`<served_model_name>.pkl`）。
- 启动期路径校验（fail-fast）：文件夹模式目录必须预先存在；文件模式父目录必须预先存在；
  文件本身可在首次保存时创建。不存在→报错终止启动，提示「保存的路径不存在」。
  值非绝对路径→启动期报错（与其它 env 校验一致）。
- 异常编号：落盘开启时，首次保存读文件取 max(内存计数器, 文件 key 最大值) 同步计数器后累加
  （防重启覆盖）；落盘关闭时仅内存计数器累加（重启归零，无文件可覆盖）。
- 保存不阻塞客户端：检测在响应发完后 fire-and-forget 调度；磁盘写经 `run_in_executor` offload
  到线程，`asyncio.Lock` 串行化，事件循环不阻塞。
- 保存失败 → log，不影响客户端/检测/后续请求，不回退编号。
- `/anomaly/metrics` 新增 `vllm_anomaly_last_id{model}` 与 `vllm_anomaly_last_timestamp_seconds{model}`
  两个 Gauge，不改动现有指标/标签。

**验收**
- 设 `VLLM_ANOMALY_SAVE_PATH=/data/save`（目录存在）→ 首次异常产生 `/data/save/<model>.pkl`，
  内含 `{1: {time,prompt,ill_type,topk_logprobs,tokens_ids,text,model_name}}`；重启后再异常编号从
  文件 max-key+1 续接（不覆盖）。
- 设 `VLLM_ANOMALY_SAVE_PATH=/data/x.pkl`（父目录存在）→ 落 `/data/x.pkl`。
- 目录/父目录不存在 → vllm serve 启动失败，提示「保存的路径不存在」。
- 不设 env → 服务正常启动；异常仍编号（内存，重启归零）；`/anomaly/metrics` 出现
  `vllm_anomaly_last_id` / `vllm_anomaly_last_timestamp_seconds`。
- 单请求多候选多异常 → 多条记录、多个编号，互不覆盖。
- 保存失败 → log，客户端响应/检测/后续请求不受影响。
- 现有指标结构、webui 解析不受影响。

### 2.17 动态配置端点

中间件在可配置路径（`VLLM_ANOMALY_CONFIG_PATH`，默认 `/anomaly/config`）内联响应：

- **GET**：返回当前 `{"monitor_rate": <float>}`。
- **POST** `{"monitor_rate": <float>}`：校验 ∈ [0.0, 1.0]，更新运行时监控概率，
  返回 `{"monitor_rate": <float>}`。校验失败返回 400 + `{"error": <msg>}`，
  当前值不变。
- 其他 method → 透传下游。
- `enabled=False` 时端点仍可达。
- 无认证（同 metrics 端点）。
- 不持久化。

**验收**
- GET `/anomaly/config` → 200 + `{"monitor_rate": <当前值>}`。
- POST `{"monitor_rate": 0.3}` → 200 + `{"monitor_rate": 0.3}`，下一请求按 0.3 采样。
- POST `{"monitor_rate": 1.5}` → 400，当前值不变。
- POST 非 JSON → 400，当前值不变。
- `enabled=False` → GET 仍可达。
- 重启 → 回退 env 初始值。
- `GET /anomaly/metrics` 含 `vllm_anomaly_monitor_rate` gauge，反映当前值。

## 3. 输入输出契约

### 3.1 构造与调用

- `AnomalyMiddleware(app)`：`app` 为下游 ASGI 可调用。
- `async def __call__(self, scope, receive, send)`：纯 ASGI。

### 3.2 请求侧

- 读请求体：聚合所有 `http.request` 消息至 `more_body=False`（处理 `http.disconnect`）。
- 重放 receive：构造 receive 包装函数；首次调用返回合成单条 `http.request`(body, more_body=False)；后续调用委托原始 `receive()` 获取后续消息（如 `http.disconnect`）。禁止二次读返回空 body 消息，否则 vllm 会重复处理请求。
- 请求 scope：浅拷贝并改写 `content-length` header。

### 3.3 响应侧

- `http.response.start`：注入 `x-anomaly-request-id`；流式立即发；非流式缓冲后发（并 patch 响应 content-length）。
- `http.response.body`：流式增量转发；非流式缓冲至 `more_body=False` 再处理。
- 终端 body 后置 `_finished`，忽略后续终端消息。

### 3.4 检测数据

- 非流式：`extract_*_response(data) -> list[(np.ndarray, np.ndarray)]`（per choice: logprobs, token_ids）。
- 流式：`SSEStreamProcessor.get_detection_data() -> (logprobs_all, token_ids_all)`。
- 调度：`schedule_detection(runner, logprobs_list, token_ids_list, *, request_id, model, metrics, pending_tasks)`
  分配 SharedMemory + 写入两数组 + 构造元数据（buffer 名称 + 形状 + 偏移 + choice_lengths）后提交检测。
  `model` 仅用于指标标签，不参与检测。

### 3.5 指标

- `render_metrics() -> bytes`；`METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"`。
- 标签：`ill_type`(0-4)、`model`(来自请求 `model`，缺失用 `"unknown"`)。

## 4. 行为约束与不变量

1. **透明无条件**： `enabled=True` 和异常监控概率共同作用，决定请求是否注入、响应恢复和检测。
   `monitor_rate` 为运行时可变（`POST /anomaly/config` 更新 `self._monitor_rate`），
   其他配置仍为启动期不可变。
2. **降级即透传**：`enabled=False`（master 开关 off）→ 不读 body、
   不注入、不拦截；指标端点独立可达。启动期检测器不可构造 → 报错终止启动（不进入透传降级）。
3. **top_logprobs 跨请求恒定**：默认 20，可配 1-20，但运行期不可变。
   理由：保证每 token 的 top-logprobs 条目数一致，检测语义稳定。
4. **检测不传模型配置**：检测数据为 `(logprobs, token_ids)` 2-元组（numpy 数组）；
   `tokens` 不再单独传递，由 `token_ids[:, 0]`（top-1 after stable sort）派生。
   `model` 仅用于指标标签，不传入检测算法。token 类别映射在启动期从 tokenizer 生成并经进程池 initializer 注入检测器。
5. **检测并行（多进程隔离）**：多请求检测调用经 `ProcessPoolExecutor` 多进程并行执行，
   每工作进程持独立 `ILLDetector` 实例（进程隔离 `_garbled_count` 等内部状态）。
   单请求多候选输出在各候选独立检测。
6. **检测不阻塞客户端**：检测在响应全发后调度，fire-and-forget，异常全捕获。
7. **`token_id:` 限制**：未设 `return_tokens_as_token_ids=True` 时，恢复后响应默认不得含 `token_id:` 前缀；
   文本经 `TokenTextResolver`（§2.15）还原，resolver 优先、`bytes` 兜底、缺失置 null，**绝不直接回写**。
   **例外**：客户端请求了 topk（chat `logprobs=True && top_logprobs>0`；completions `logprobs>0`）**且**
   单 token decode 失败时，受影响字段回退 `token_id:NNN` 输出（保留 topk logprob 数据），chat 主 token 仍
   优先 bytes 兜底（三层：resolver→bytes→`token_id:NNN`）。该降级不影响检测与客户端响应完整性。
8. **流式不缓冲**：纯 ASGI，SSE 增量转发，不缓冲整流，保持流式响应原始机制。
9. **指标隔离**：独立 CollectorRegistry，不与下游 `/metrics` 混用。
10. **启动期硬依赖 fail-fast**：`configs/detector.yaml`、env 变量、tokenizer 加载、ILLDetector 构造——
    启动期失败即 raise 终止启动，不得进入降级模式。
11. **检测优先原则**：检测器工作进程的错误不影响其他工作进程——log + 计 error + 不降级算法 +
    不设 `enabled=False` + 不标记全局不可用 + 后续请求继续检测。
12. **tokens 派生**：`tokens` 不再单独传递，由 `token_ids[:, 0]`（top-1 after stable sort）派生，
    用于 n-gram/滑窗/轨迹检测。

## 5. 验收标准总览

- 非目标路径/方法透传不改 body。
- 注入改 body 与请求 `Content-Length`；恢复按原始参数 null/截断/文本还原。
- chat 和 completions 无 `token_id:` 泄漏，未请求 return_tokens_as_token_ids 时 经
  `TokenTextResolver`（§2.15）将 token_id 转为 token 文本；单 token decode 失败时按 §4.7 例外分流：
  触发例外（客户端请求 topk）→ 回退 `token_id:NNN` 保留 topk logprob 数据；未触发 → 回退
  null/bytes（带 `token_id:` 泄漏守卫），全文仍无 `token_id:`、检测正常。
- 流式增量转发 + 跨块事件重组 + `[DONE]`/keep-alive 透传 + logprobs 和 token_id 缓存+ 流式推理结束后调用检测。
- 异常监控概率 0.0 不检测/1.0 全检测。
- `x-anomaly-request-id` 头存在且唯一。
- 内联 metrics 200 + 正确 content-type + Prometheus 文本；下游无路由也作答，不报错，详细记录至日志。
- 启动期 fail-fast：`configs/detector.yaml` 缺失 / env 变量非法 / tokenizer 加载失败 / ILLDetector 构造失败
  → vllm serve 启动失败并报错，不进入降级模式。
- `VLLM_ANOMALY_ENABLED=0` → 服务正常启动（纯透传，不校验 detector/tokenizer），指标端点报零值。
- tk2cat 生成失败 → 服务正常启动（无词表检测模式，记 WARNING）。
- tokenizer 加载失败时，vllm serve 启动失败并报错。
- `VLLM_ANOMALY_DETECTOR_WORKERS` 默认 4，可配置（须 ≥1）。
- 多请求并行检测时，各候选独立检测，结果正确。
- 工作进程异常时，不影响其他进程，异常计数 +1，池自动重建。
- 推理期检测器异常 → 计 error，客户端不受影响，异常情况详细记录至日志，后续请求正常检测。
- 单插件部署，构造 `(app)` 无 kwargs。
