# accuracy-monitoring 测试报告

> **项目**: accuracy-monitoring — 推理精度异常检测工具
> **版本**: v0.2.0
> **日期**: 2026-09-08
> **测试执行**: OpenCode + Pytest（离线单测/集成 386 例 + 真实 vLLM 服务器 E2E 框架 42 例）
> **部署模型**: Qwen3-0.6B（Atlas 910B4 × 1，E2E 轮次）

---

## 1. 测试概述

### 1.1 测试目标

依据 `spec.md`（§2.1–§2.17 功能需求 + §3 契约 + §4 行为不变量 + §5 验收标准）与
`design.md`，验证中间件的完整性与正确性：

- **请求拦截**：仅拦截 `/v1/chat/completions`、`/v1/completions`，其余路径/方法/非 HTTP 原样透传
- **强制采集**：强制注入 `logprobs`/`top_logprobs`/`return_tokens_as_token_ids` 并修正 Content-Length
- **客户端透明恢复**：`logprobs=null`/截断/文本还原 三级兜底，未设 `return_tokens_as_token_ids` 时**绝不泄漏 `token_id:`**
- **流式安全转发**：SSE 增量转发、跨块事件重组、`[DONE]`/keep-alive 透传、检测数据跨块累积
- **异常检测算法**：生僻字(1)/乱码(2)/重复(3)/NaN(4) 四类异常可检出，正常样本零误报
- **检测调度**：fire-and-forget、失败隔离计 error、多进程并行、空响应跳过、多候选不覆盖
- **异常信息本地保存**（v0.2.0 增强）：检出异常现场落盘 pickle，含输出 token id 序列与思维链文本
- **动态配置端点**（v0.2.0 新增）：`GET/POST /anomaly/config` 运行时查询/更新监控概率，非法值 400 且不生效
- **监控概率采样**：0.0 透传 / 1.0 全检 / 0.3 概率注入 / 运行时动态调整
- **关联标识**：`x-anomaly-request-id` 响应头唯一
- **Prometheus 指标**：独立 registry + 按 `ill_type`/`model`/`choice_index` 上报 + 四 gauge + `vllm_anomaly_monitor_rate`
- **优雅降级**：配置非法/检测器不可用/路径缺失 → 永久透传 + 指标报零 + 日志事件
- **tokenizer 获取链**：env → argv(--tokenizer/--model) → model_hint → /v1/models → HF 缓存扫描
- **WebUI**（v0.2.0 增强）：多实例聚合、阈值告警、历史数据导入、配置热重载

E2E 测试分层执行：**lightweight**（PR 触发，P0 子集）/ **full**（P0+P1+P2）/ **nightly**（全量）。

### 1.2 测试结果汇总

| 指标 | 结果 |
|------|------|
| 离线单测/集成（本轮，2026-09-08） | **386** |
| 通过 | **374** |
| 跳过（live 集成，需真实 vLLM 服务） | **12** |
| 失败 | **0** |
| 真实服务器 E2E（42 例，TC-001~TC-042） | v0.1.0 轮全通过；本轮环境无 vLLM/模型，**待真机复测** |
| 发现 Bug | **0** |

> 本轮（v0.2.0）为离线环境执行：单元/集成测试全绿；E2E（`tests/e2e/tests/`，需真实 vLLM 进程与本地模型文件）
> 本轮未执行——失败/报错均为 `FileNotFoundError: 'vllm'` 环境缺失所致，非代码缺陷。

---

## 2. 测试环境

### 2.1 本轮环境（v0.2.0，离线单测/集成）

| 项目 | 配置 |
|------|------|
| 操作系统 | Linux (aarch64) |
| Python | 3.12.4（pytest 9.1.1） |
| 执行方式 | `python3 -m pytest tests --ignore=tests/e2e`（ASGI mock 传输，无真实 vLLM 进程） |

### 2.2 E2E 环境（v0.1.0 轮，2026-08-21，历史记录）

| 项目 | 配置 |
|------|------|
| 操作系统 | Linux (aarch64) |
| NPU | Huawei Atlas 910B4 × 2（device 2 & 5） |
| Python | 3.11.15 |
| vLLM / vLLM-Ascend  | 0.20.2 |
| torch / torch_npu | 2.10.0 |
| transformers | 5.5.3 |
| numpy / PyYAML / prometheus_client / httpx / pytest | 1.26.4 / 6.0.3 / 0.25.0 / 0.28.1 / 9.1.1 |
| 测试模型 | Qwen3-0.6B |
| E2E 框架 | Orchestrator + VllmLauncher + InjectorServer(sidecar) + PrometheusClient + BaselineStore |
| E2E 部署 | `vllm serve --middleware anomaly_middleware.AnomalyMiddleware`（真实 vLLM 进程） |
| E2E 注入 | InjectorServer（127.0.0.1:9999）覆写 `run_async`/`extract_empty`/`sse_keepalive` 注入异常数据 |

---

## 3. 功能点覆盖矩阵（spec §2 → 测试用例）

| spec 章节 | 功能点 | 单元/集成测试 | E2E 用例 | 用例数 |
|-----------|--------|----------|----------|:------:|
| §2.1 请求拦截 | 非目标路径/方法透传、非 HTTP 透传、GET /v1/models、GET chat | `test_middleware_helpers.py` | `test_baseline_collection.py` | 6 |
| §2.2 强制采集 | chat/completions 注入、max(客户端,N)、Content-Length 修正 | `test_extractor.py` | `test_top_logprobs_max_rule.py` | 12 |
| §2.3 响应恢复 | null/截断/文本还原/保留 token_id/n>1 循环 | `test_extractor.py` | `test_transparency_*.py`（4 变体 × 3 请求形态） | 24 |
| §2.4 流式 | 增量转发、跨块重组、[DONE]/keep-alive/CRLF、无 DONE flush、非 JSON 透传、多 data 行、附加字段、多分块 | `test_sse.py` | `test_transparency_*_stream.py` + `test_stream_no_done.py` + `test_keep_alive_passthrough.py` + `test_client_disconnect.py` | 31 |
| §2.5 异常检测 | 四类异常检出、空响应跳过、n>1 不覆盖、长序列重复 | `test_detector_illtypes.py` | `test_inject_*.py` + `test_normal_no_false_positive.py` + `test_empty_data_boundary.py` + `test_multi_choice_n3.py` | 17 |
| §2.6 失败隔离 | 检测异常不影响客户端、计 error、错误响应状态/body 保留 | `test_detector_runner.py` + `test_middleware_helpers.py` + `test_e2e_sampling_metrics_degrade.py` | `test_process_pool_partial_detection_error.py` | 9 |
| §2.7 多进程并行检测 | 多进程并行、单请求多候选独立上报 | `test_detector_runner.py` | `test_concurrent_10_parallel.py` + `test_multi_choice_n3.py` | 5 |
| §2.8 监控概率 | 0.0/1.0/0.3 概率注入、动态更新（§2.17 联动） | `test_dynamic_config.py` + `test_e2e_sampling_metrics_degrade.py` | `test_monitor_rate_passthrough.py` + `test_monitor_rate_full_inject.py` | 2 |
| §2.9 关联标识 | 响应头唯一（Mock + live） | `test_middleware_helpers.py` | `test_repeat_request.py` | 3 |
| §2.10 指标 | 200 + content-type、独立 registry、choice_index、四 gauge、`monitor_rate` gauge、下游无路由 | `test_metrics.py` + `test_anomaly_save.py` + `test_e2e_sampling_metrics_degrade.py` | `test_metrics_endpoint_check.py` + `test_metrics_no_leak.py` | 14 |
| §2.11 env 配置 | 默认值、覆盖、非法值降级、边界回退、tokenizer_model、config_path | `test_config.py` + `test_middleware_preheat.py` + `test_e2e_sampling_metrics_degrade.py` | `test_enabled_off_passthrough.py` + `test_explicit_tokenizer.py` | 16 |
| §2.12 配置路径 | 存在/缺失 → fail-fast | `test_config.py` | `test_detector_yaml_missing.py` | 4 |
| §2.13 优雅降级 | 检测器不可用 → 永久透传 + 指标报零；推理期降级不改变客户端响应 | `test_middleware_helpers.py` + `test_detector_runner.py` + `test_e2e_sampling_metrics_degrade.py` | `test_enabled_off_passthrough.py` + `test_inf_logprob_boundary.py` + `test_process_pool_partial_detection_error.py` | 7 |
| §2.14 插件部署 | `--middleware` 单参构造 | — | `test_baseline_collection.py`（真实部署） | 1 |
| §2.15 TokenTextResolver | resolve/缓存、7 级获取链、降级分流、trust_remote_code、argv 解析 | `test_token_resolver.py` + `test_token_categorizer.py` + `test_middleware_preheat.py` | `test_explicit_tokenizer.py` | 39 |
| §2.16 异常信息本地保存 | AnomalyStore + env save_path + record_anomaly + runner 保存联动（含 text_tokenid/reasoning_content） | `test_anomaly_save.py` | — | 33 |
| §2.17 动态配置端点 | GET 查询 / POST 更新 monitor_rate、非法值 400、不持久化、enabled=False 可达 | `test_dynamic_config.py` | — | 15 |
| §3 契约 | 请求体聚合/重放、scope 拷贝、响应 start/body 处理、终端幂等 | `test_middleware_helpers.py` | — | 8 |
| WebUI（webui_README） | 多实例 API、告警、配置热重载、store 分层趋势、认证、事件、全链路 | `test_webui_*.py`（8 文件） | — | 116 |
| ASGI mock 全链路 | chat/completions × 流/非流端到端 + 检测联动 + live 探测 | `test_e2e_chat.py` + `test_e2e_completions.py` + `test_e2e_detection.py` + `test_e2e_streaming.py` + `test_e2e_live.py` | — | 40 |

> 覆盖结论：**spec.md 全部 17 个功能需求章节均有对应测试**，覆盖检测算法输出路径（四类异常）、
> 异常落盘、动态配置端点、WebUI 单元/集成、ASGI mock 全链路、错误状态透传、SSE 多行/多分块、
> completions n>1、并发检测等场景。

---

## 4. 单元/集成测试（离线，共 386：374 PASS + 12 SKIP-live）

### 4.1 数据层：请求快照 / 注入 / 抽取 / 恢复（test_extractor.py，37）

| 分组 | 覆盖 | 结果 |
|------|------|:----:|
| parse_token_id | `token_id:NNN`/纯数字/int/bool/非法 | PASS |
| save_original_params | chat/completions 默认值与 n/stream 快照 | PASS |
| inject_params | 注入字段、max(客户端,N) 双向、缺省 | PASS |
| extract_chat/completions | per-choice 抽取、topk_n 截断、None 位置容错 | PASS |
| extract_*_text_tokenids | per-choice 输出 token id 序列抽取（非流式，v0.2.0 新增） | PASS |
| strip_chat/completions | null/截断/文本还原/保留 token_id/n>1/resolver 三级兜底/降级例外 | PASS |

### 4.2 检测算法（test_detector_illtypes.py 18 + test_detector.py 11，共 29）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| test_detect_rare_character_* / no_vocab_top1 | 生僻字 ill_type=1（带词表类别判定 + 无词表 top1 降级 + 类别过滤假阳性） | PASS |
| test_detect_garbled_* | 乱码 ill_type=2（logp 区间区分 rare/garbled、占比阈值触发/不触发） | PASS |
| test_detect_repetition_* | 重复 ill_type=3（窗口阈值；短序列不误报） | PASS |
| test_detect_nan_value / inf_value | NaN/Inf ill_type=4 | PASS |
| test_run_multi_request_isolation | 多请求并行，单异常不串扰 | PASS |
| 配置校验 / 词表注入 / topk_n | 检测器配置、词表注入、topk_n | PASS |
| 辅助：get_ngrams/get_distinct_n/sliding_window/乱码状态 | 窗口与状态机 | PASS |

### 4.3 检测调度（test_detector_runner.py 8 + test_middleware_preheat.py 3，共 11）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| run_sync / run_async | 正常执行 | PASS |
| construction_failure / unusable | 构造失败 → 永久 unusable 快速失败 | PASS |
| schedule_detection（正常/异常） | fire-and-forget、error 计数、done_callback 出集 | PASS |
| serialized_single_worker / topk_n / set_vocabulary | 串行化、topk_n 注入、词表懒注入 | PASS |
| 预热线程 + tk2cat 注入 + 竞态补调 | eager 初始化（裁剪后子集） | PASS |

### 4.4 指标（test_metrics.py，13）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| test_metrics_content_type | 端点 200 + content-type | PASS |
| test_record_detection_normal_only_requests / anomaly_choice_index | detected_total 按 ill_type/model/choice_index 上报 | PASS |
| test_record_detection_nan_type / repetition_type | 异常类型标签 | PASS |
| test_record_detection_accumulates_requests_per_request | 每请求累计 requests_total | PASS |
| test_record_error | error 计数 | PASS |
| test_record_detection_unknown_model_label | 未知 model 标签兜底 | PASS |
| test_registry_isolated_from_default | 独立 registry，不影响全局 | PASS |
| test_record_detection_does_not_raise_on_bad_input | 非法输入容错 | PASS |
| monitor_rate gauge（v0.2.0 新增） | `vllm_anomaly_monitor_rate` 设置/渲染 | PASS |

### 4.5 tokenizer 解析（test_token_resolver.py 19 + test_token_categorizer.py 10，共 29）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| test_resolve_* | resolve 缓存、未知/异常/None 返回 None | PASS |
| test_acquire_tokenizer_* | tokenizer 获取链（env/argv/root/served/cache-scan 优先级与降级） | PASS |
| test_from_pretrained_* | trust_remote_code 默认与显式覆盖 | PASS |
| test_parse_vllm_argv_* | argv 解析（--flag= 与 --flag value、值型 flag 防误认、host/port） | PASS |
| test_poll_model_root / gives_up_on_timeout | /v1/models 轮询（成功/超时） | PASS |
| test_generate_tk2cat_* / test_get_decode_fn_* | tk2cat 生成与 decode 函数选择 | PASS |

### 4.6 中间件分派与助手（test_middleware_helpers.py，17）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| _read_all_body | 单块/多块/disconnect | PASS |
| _make_replay_receive | 首次合成 body、二次不返回空 http.request | PASS |
| _patch_scope_content_length | 改写/补加/浅拷贝隔离 | PASS |
| 分派 | 非 HTTP/GET metrics/GET models/GET chat/disabled/动态配置端点透传 | PASS |
| 错误响应透传 | 400+错误 JSON → 状态码/消息保留、不调度检测；500+非 JSON → 原样透传 | PASS |
| 构造期降级 | env 非法（top_logprobs=0）→ config.enabled=False | PASS |
| 终端 body 幂等 / _ensure_resolver | 终端后重复 body 忽略；acquire 缓存、失败返回 None | PASS |

### 4.7 SSE 流式处理器（test_sse.py，15）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| [DONE]/keep-alive/非 JSON/CRLF | 原样透传 | PASS |
| 跨块重组 | 半事件缓冲、补齐后单条输出 | PASS |
| 多块累积 + 每块恢复 | 检测数据不受客户端 M 截断、n=3 按 choice.index 独立成组 | PASS |
| 多 data 行 / 附加字段 / 多分块 / flush 无 DONE | 透传、event:/id: 保留、逐字节喂入、排空残余 | PASS |

### 4.8 配置（test_config.py，16）

| 测试 | 覆盖 | 结果 |
|------|------|:----:|
| test_config_defaults / env_override | 默认值与 env 覆盖（含 `VLLM_ANOMALY_CONFIG_PATH`，v0.2.0 新增） | PASS |
| test_config_invalid_top_logprobs / _high | top_logprobs 1-20 校验（0/21 拒绝） | PASS |
| test_config_invalid_monitor_rate / _boundaries_valid | monitor_rate 校验（1.5 拒绝 / 0.0、1.0 边界合法） | PASS |
| test_resolve_config_path_* | detector 路径存在/缺失 | PASS |
| test_tokenizer_model_* | tokenizer_model env | PASS |
| workers=0 回退 / enabled 非法串 / metrics_path 空白 | 边界回退 | PASS |

### 4.9 异常信息本地保存（test_anomaly_save.py，33，v0.2.0 新增）

| 分组 | 覆盖 | 结果 |
|------|------|:----:|
| AnomalyStore | 编号分配、文件/文件夹模式、pkl 内容字段（含 `text_tokenid`/`reasoning_content`） | PASS |
| env save_path | 未设不落盘、`.pkl` 文件模式、目录模式、启动期 fail-fast 校验 | PASS |
| 落盘联动 | record_anomaly + detector_runner 保存逻辑（fake runner，不依赖 pyyaml/transformers） | PASS |

### 4.10 动态配置端点（test_dynamic_config.py，15，v0.2.0 新增）

| 分组 | 覆盖 | 结果 |
|------|------|:----:|
| GET | 返回当前 `{"monitor_rate": <float>}` | PASS |
| POST 合法值 | ∈ [0.0, 1.0] 更新运行时值 + gauge 同步 | PASS |
| POST 非法值 | 1.5/非 JSON → 400 且当前值不变 | PASS |
| 端点行为 | 默认路径、`VLLM_ANOMALY_CONFIG_PATH` 自定义、enabled=False 仍可达、不持久化 | PASS |

### 4.11 采样/指标/降级集成（test_e2e_sampling_metrics_degrade.py，15，v0.2.0 新增）

| 分组 | 覆盖 | 结果 |
|------|------|:----:|
| 采样 | monitor_rate 0.0/1.0/0.3 注入行为（spec §2.8） | PASS |
| 指标 | 请求/检出/错误计数与 gauge（spec §2.10） | PASS |
| 降级与隔离 | 检测异常隔离、推理期降级不影响客户端（spec §2.6/§2.13） | PASS |

### 4.12 ASGI mock 全链路（test_e2e_chat.py 8 + test_e2e_completions.py 6 + test_e2e_detection.py 5 + test_e2e_streaming.py 9，共 28）

| 分组 | 覆盖 | 结果 |
|------|------|:----:|
| chat | 非流式注入/恢复/透明、流式增量与 [DONE] 后检测 | PASS |
| completions | 非流式/流式、token_id 还原、n>1 | PASS |
| detection | 四类异常检出与指标联动 | PASS |
| streaming | 跨块重组、检测数据累积、无缓冲透传 | PASS |

### 4.13 live 集成（test_e2e_live.py，12 — 全部 SKIP）

| 说明 | 结果 |
|------|:----:|
| 需真实 vLLM 服务（`http://127.0.0.1:8008`，`run_server.sh` 启动）；离线环境不可达自动 skip，不阻塞 | SKIP ×12 |

### 4.14 WebUI（test_webui_*.py 8 文件，共 116，v0.2.0 新增/扩充）

| 文件 | 用例 | 覆盖 | 结果 |
|------|:----:|------|:----:|
| test_webui_api.py | 23 | 认证门槛、登录、summary/instances/events/trends 结构与权限、增删/暂停恢复/409/404、删除清数据 | PASS |
| test_webui_config.py | 20 | 配置加载/校验/热重载、yaml 外部修改生效 | PASS |
| test_webui_alerts.py | 20 | 滑动窗口阈值告警、多渠道通知（webhook/邮箱）、去抖 | PASS |
| test_webui_collector.py | 17 | 多实例轮询采集、解析 /anomaly/metrics、离线实例处理 | PASS |
| test_webui_store.py | 12 | 环形缓冲、purge_instance、分层趋势（原始点/分钟桶）与全局聚合、导入/清除 | PASS |
| test_webui_auth.py | 10 | 登录/登出、会话、密码校验、防泄漏 | PASS |
| test_webui_events.py | 9 | 事件流、容量、时间序 | PASS |
| test_webui_e2e.py | 5 | 真实本地上游 + webui 后台轮询 → 事件 → 告警 → webhook 全链路；加实例立即可见；热重载生效 | PASS |

---

## 5. 真实服务器 E2E（vLLM，42 例，TC-001~TC-042）

> **本轮（v0.2.0）状态：未执行**。E2E 由 `tests/e2e/run_e2e.py` 驱动，需真实 vLLM 进程与本地模型
> （`/home/gyl/models/Qwen3-0.6B`）；本轮环境无 vLLM 可执行文件与模型，42 例全部因
> `FileNotFoundError: 'vllm'` 无法启动服务而未运行（环境缺失，非代码缺陷）。**待真机（Atlas 910B4 + Qwen3-0.6B）复测。**
>
> 以下为 **v0.1.0 轮（2026-08-21）历史结果**：42 用例全部 PASS、零检测错误。框架与用例集本轮无变更
> （用例见 `tests/e2e/cases_registry.yaml`，结果报告输出至 `tests/e2e/reports/`）。

### 5.1 框架概述

| 组件 | 职责 |
|------|------|
| **Orchestrator** | 顶层入口（`run_e2e.py`），解析 `--tier`/`--models`/`--local`，驱动 pytest |
| **VllmLauncher** | 真实 vLLM 进程管理：`vllm serve <model> --middleware ...`，启动健康检查（`/v1/models` 轮询），进程停止/清理 |
| **vllm_service_factory** | session 级单活跃实例工厂：签名（model/middleware/env/injector/expect_fail）相同复用、不同换出先停后启；fail-fast 用例不占活跃槽 |
| **InjectorServer** | sidecar 注入器（127.0.0.1:9999）：`set_override(hook, payload, count)` 覆写 `run_async`/`extract_empty`/`sse_keepalive`，注入确定性异常数据（rare/garbled/repetition/nan/inf/detection_error） |
| **PrometheusClient** | 指标轮询客户端：`get_counter`/`get_gauge`/`wait_for`（超时轮询），从 `/anomaly/metrics` 解析 Prometheus 文本 |
| **HttpClient** | OpenAI 兼容 HTTP 客户端：chat/completions 非流式 + chat_stream/completions_stream 流式 + post_raw |
| **OpenAISdkClient** | 官方 OpenAI SDK 客户端（TC-032 兼容性验证） |
| **BaselineStore** | 文件基线存储：无中间件服务采集 12 组基线（chat/completions × 流/非流 × v1/v2/v3 变体），有中间件响应逐结构透明比较 |
| **compare.py** | 透明性比较：`assert_response_transparent`（非流式）/`assert_stream_transparent`（流式重建后比较），忽略 id/created，logprob 浮点不参与，公共前缀严格 + 尾部容差 |

- **执行环境**: root=True, HOME 隔离，端口自动选择（或 `VLLM_E2E_PORT` 指定）
- **测试模型**: Qwen3-0.6B（`tests/e2e/models/qwen3-0.6b.yaml`：tensor_parallel_size=1, dtype=float16, max_model_len=8192）
- **分层**: lightweight（PR 触发，11 例 P0）/ full（37 例 P0+P1+P2）/ nightly（42 例全量）
- **请求变体**: v1（无 logprobs）/ v2（logprobs+top5）/ v3（logprobs+top5+rtati）三种采集参数组合

### 5.2 v0.1.0 轮结果汇总（历史）

| 指标 | 值 |
|------|------|
| **PASS** | **42** |
| **FAIL** | **0** |
| **SKIP** | **0** |
| **TOTAL** | **42** |
| **通过率** | **100%** |
| 检测错误累计 | **0** |

### 5.3 分类统计（v0.1.0 轮，历史）

| 分类 | 说明 | PASS | FAIL |
|------|------|:----:|:----:|
| TRANSP | 透明性验证（与无中间件基线逐结构比较，chat/completions × 流/非流） | 5 | 0 |
| DETECT | 异常检测（四类异常注入检出 + 正常零误报 + 空响应跳过 + 多候选不覆盖） | 7 | 0 |
| METRICS | Prometheus 指标（端点 200 + content-type + 独立 registry 不泄漏） | 2 | 0 |
| CONFIG | 配置与环境变量（monitor_rate/top_logprobs/enabled/metrics_path/tokenizer） | 6 | 0 |
| FAILFAST | 启动期 fail-fast（配置非法/文件缺失/workers=0 → 服务启动失败） | 4 | 0 |
| BOUND | 边界值（空响应/max0/单 token/topk 最小/inf logprob/空数据） | 5 | 0 |
| EDGE | 边缘场景（非 JSON/非 dict/非法模型/无 DONE/keep-alive/断连/重复/n3） | 8 | 0 |
| SEC | 安全（恶意 prompt 越狱不触发误报） | 1 | 0 |
| COMPAT | 兼容性（OpenAI SDK chat 流/非流） | 1 | 0 |
| PERF | 性能（延迟开销 < 200ms / 10 并发全部 200） | 2 | 0 |
| STAB | 稳定性（2000 次请求 + RSS ≤ 2×） | 1 | 0 |
| RES | 韧性/自愈（进程池崩溃恢复 + 部分检测错误隔离） | 2 | 0 |

### 5.4 用例明细（v0.1.0 轮，历史——TC 编号/覆盖/优先级/分层保持不变）

42 例明细（TC-001~TC-042，含 TRANSP/DETECT/METRICS/CONFIG/FAILFAST/BOUND/EDGE/SEC/COMPAT/PERF/STAB/RES
十二类逐条覆盖描述）见上一版报告（v0.1.0，2026-08-21）；本轮用例集与分层未变更，
复测后按本轮结果刷新本节。

> **E2E 累计（v0.1.0 轮）**：42 用例全部通过，零检测错误。透明性验证确认中间件对客户端完全透明（响应结构、
> 生成内容、logprobs 截断/还原与无中间件基线逐结构一致）。四类异常注入检出 + 正常零误报验证检测正确性。
> fail-fast 4 例确认启动期硬依赖失败即终止启动。性能 < 200ms 开销 + 10 并发 + 2000 次长稳定性验证无退化。

---

## 6. 测试文件清单

| 文件 | 用例数 | 层级 | 职责 |
|------|:------:|------|------|
| tests/test_extractor.py | 37 | Tier 0 | 快照/注入/抽取/恢复（含 n>1、降级例外、text_tokenids 抽取） |
| tests/test_anomaly_save.py | 33 | Tier 0 | 异常落盘（AnomalyStore + save_path + 保存联动，v0.2.0 新增） |
| tests/test_webui_api.py | 23 | Tier 0 | WebUI REST API（认证/CRUD/暂停恢复，v0.2.0 新增） |
| tests/test_webui_config.py | 20 | Tier 0 | WebUI 配置与热重载（v0.2.0 新增） |
| tests/test_webui_alerts.py | 20 | Tier 0 | 阈值告警与多渠道通知（v0.2.0 新增） |
| tests/test_token_resolver.py | 19 | Tier 0 | resolver + tokenizer 获取链 + argv 解析 |
| tests/test_detector_illtypes.py | 18 | Tier 0 | 四类异常检出 + 假阳性控制 + 窗口状态机 |
| tests/test_webui_collector.py | 17 | Tier 0 | 多实例指标轮询采集（v0.2.0 新增） |
| tests/test_middleware_helpers.py | 17 | Tier 0 | ASGI 助手 + 分派 + 错误透传 + 降级 + 终端幂等 |
| tests/test_config.py | 16 | Tier 0 | env 配置校验 + 边界回退 + 路径解析（含 config_path） |
| tests/test_sse.py | 15 | Tier 0 | SSE 跨块重组/透传/多行/附加字段/多分块 |
| tests/test_dynamic_config.py | 15 | Tier 0 | 动态配置端点 GET/POST（v0.2.0 新增） |
| tests/test_e2e_sampling_metrics_degrade.py | 15 | Tier 0 | 采样/指标/降级/检测异常隔离集成（v0.2.0 新增） |
| tests/test_metrics.py | 13 | Tier 0 | 指标记录/渲染/独立 registry/monitor_rate gauge |
| tests/test_webui_store.py | 12 | Tier 0 | 环形缓冲/分层趋势/导入（v0.2.0 新增） |
| tests/test_detector.py | 11 | Tier 0 | 检测器配置/词表注入/topk_n |
| tests/test_webui_auth.py | 10 | Tier 0 | WebUI 认证（v0.2.0 新增） |
| tests/test_token_categorizer.py | 10 | Tier 0 | 分类函数 + generate_tk2cat 降级链 |
| tests/test_webui_events.py | 9 | Tier 0 | WebUI 事件流（v0.2.0 新增） |
| tests/test_detector_runner.py | 8 | Tier 0 | 进程池/共享内存/调度/异常隔离 |
| tests/test_middleware_preheat.py | 3 | Tier 0 | 预热线程 + tk2cat 注入 + 竞态补调 |
| tests/test_e2e_chat.py | 8 | Tier 1 | ASGI mock 全链路 chat（非流式/流式） |
| tests/test_e2e_streaming.py | 9 | Tier 1 | ASGI mock 全链路流式专项 |
| tests/test_e2e_completions.py | 6 | Tier 1 | ASGI mock 全链路 completions |
| tests/test_e2e_detection.py | 5 | Tier 1 | ASGI mock 全链路检测联动 |
| tests/test_webui_e2e.py | 5 | Tier 1 | WebUI 真实本地上游全链路（轮询→事件→告警→webhook） |
| tests/test_e2e_live.py | 12 | Tier 1 | live 集成（需真实 vLLM :8008，离线 skip） |
| tests/e2e/tests/*.py（42 文件） | 42 | Tier 2 | 真实 vLLM 服务器 E2E（TC-001~TC-042，本轮未执行） |
| tests/e2e/conftest.py | — | Tier 2 | E2E 框架：service_factory/injector/baseline/诊断 |
| tests/e2e/cases_registry.yaml | — | Tier 2 | 用例注册表（TC 编号/优先级/分层/order） |
| tests/e2e/models/qwen3-0.6b.yaml | — | Tier 2 | 测试模型配置（Qwen3-0.6B） |

> 合计：Tier 0/1 离线 386 例（374 PASS + 12 SKIP-live）+ Tier 2 真实服务器 E2E 42 例（待复测）。

---

## 7. 结论

v0.2.0 本轮（2026-09-08，离线环境）：**386 例单元/集成测试，374 通过、12 项 live 集成因无真实 vLLM 服务跳过、0 失败，未发现 Bug**。

- **spec.md 17 个功能需求章节全部有测试覆盖**，§5 验收标准逐项可追溯（见 §3 矩阵）；
  v0.2.0 新增能力（§2.16 异常落盘含 token id/思维链、§2.17 动态配置端点、WebUI 增强）均有专项用例。
- **历史 E2E（v0.1.0 轮，2026-08-21）**：42 例真实部署全通过（Atlas 910B4 + Qwen3-0.6B）——透明性逐结构比较、
  四类异常注入检出、正常零误报、fail-fast、性能开销 < 200ms、10 并发、2000 次长稳定性（RSS ≤ 2×）、进程池自愈。
  E2E 框架与用例集本轮无变更，**待真机复测 v0.2.0**（重点：动态配置端点与落盘新字段的真机行为）。
- **测试结论：离线全绿，功能点覆盖完整，无已知功能缺陷；E2E 复测前不建议将本轮结果外推至真实部署场景。**

### 已知限制（环境/设计约束，非缺陷）

- 离线单测不启动真实 vLLM 进程；live 集成（12 例）与 E2E（42 例）依赖本地 vLLM 服务与模型文件，不可达时自动 skip/未执行，不阻塞其余用例。
- 异常检测注入需 InjectorServer sidecar 可用（`injector.health_check()`），不可用时 `@inject` 标记用例自动 skip。
- TC-041（进程池崩溃恢复）标记 `xfail(strict=False)`：inherently flaky process-kill test，非功能缺陷。
- 透明性比较的 logprobs 浮点数值不参与相等判断（跨 run 浮点不可复现），仅比较结构量与 token 文本。

**测试结论：本轮离线测试全部通过，功能点覆盖完整，无已知功能缺陷。**
