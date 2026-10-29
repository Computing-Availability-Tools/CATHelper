# 排障指引（W5）

## 启动期 fail-fast 分类表（报错现象 → 根因 → 修复动作）

启动期硬依赖失败会**直接终止服务启动**（不降级运行）——这是设计行为，按表修复后重新拉起。

| 报错/现象 | 根因 | 修复 |
|---|---|---|
| detector.yaml 缺失提示 | 服务工作目录下无 `configs/detector.yaml` | 在仓库根目录下拉起，或确认安装完整（`pip install -e .`） |
| env 越界报错 | `monitor_rate` ∉ [0,1] / `top_logprobs` ∉ [1,20] / `detector_workers` < 1 | 按报错提示的范围修正 env |
| tokenizer 加载失败 | env / argv 自动发现 / HF 缓存扫描均失败 | 设 `VLLM_ANOMALY_TOKENIZER_MODEL` 为 `vllm serve --model` 实际值或 `--tokenizer` 值 |
| ILLDetector 构造失败 | numpy 缺失 / yaml 格式错误 / 阈值非法 | 补依赖或修正 `configs/detector.yaml` |
| 「保存的路径不存在」 | `VLLM_ANOMALY_SAVE_PATH` 目录/父目录未预存在，或非绝对路径 | 预创建目录；使用绝对路径（投递脚本会 `mkdir -p` 预创建） |
| PD：词表文件缺失/非法 | `VLLM_ANOMALY_TOKEN2CATEGORY` 路径错误或文件损坏 | 重新执行 `tools/gen_token_category.py` 生成并核对路径 |
| PD：启动器拒绝启动（已内置挂钩） | proxy 脚本已内置检测挂钩，叠加会双重集成 | 改用模式 A/B 挂载，或使用未挂钩的 proxy 脚本 |
| `VLLM_ANOMALY_ENABLED=0` 仍启动成功 | 总开关关闭 = 纯透传模式（设计行为） | 需要检测则设回 `1` 并重启 |
| `aclInit` error code 107001 + `Invalid device ID` | `ASCEND_RT_VISIBLE_DEVICES` 填了 **npu-smi 物理卡号**（该 env 只认逻辑号 0..N-1）；真实事故：容器挂 davinci2/5（逻辑 0/1），填物理 5 崩溃 | 跑 probe 取 `npu_logical_count` 与 `npu_davinci` 映射，改填逻辑号重新拉起；deploy 现已对该值做 fail-fast（rc=3） |
| `EngineCore failed to start` / `Engine core initialization failed` | 引擎核心初始化失败，根因看其上方首个 Traceback（OOM/卡号/包缺失等） | 按 Traceback 首因对照本表修复；verify 现已把该特征列入致命模式库（rc=3 即返，不再白等满窗口） |
| ansible 投递失败（连接/认证/权限拒绝） | 管理节点→计算节点通道问题，非中间件问题 | 核对 inventory/SSH/become；与精度监控无关 |

## 推理期"看不到数据"排查表（有序判读）

1. `VLLM_ANOMALY_ENABLED` 是否为 0 → 纯透传不检测；
2. `monitor_rate` 是否为 0（`GET /anomaly/config` 查询；**运行时调整过但未持久化**，重启回退 env）；
3. metrics 路径是否被改（curl 默认路径 404 ≠ 组件坏——先查 `VLLM_ANOMALY_METRICS_PATH`）；
4. 请求是否真经过中间件：V4 计数不增长 → 指向/端口错误，或请求非 `/v1/chat/completions`、
   `/v1/completions`（其他路径原样转发，不检测）；
5. `vllm_anomaly_detection_errors_total` 是否增长 → 检测执行有错误，查服务日志定位
   （设计上检测错误不影响客户端与后续请求）；
6. pkl 有无新记录：`vllm_anomaly_last_id` >0 但文件无新记录 → 检查 SAVE_PATH 与磁盘空间/权限；
   `last_id` 恒 0 → 是否确实检出过异常（`detected_total` 是否为 0）；
7. **未设置 `VLLM_ANOMALY_SAVE_PATH`** → 本来就不落盘（设计行为），如实告知并给开启步骤
   （设 env + 重启生效）。

## NPU 卡况兜底核查（probe npu_free=unknown 时的标准步骤）

probe 现已尽力解析 `npu-smi info`：输出 `npu_ids`（物理卡号）/`npu_busy_ids`（进程表有进程=占用）/
`npu_free_ids`/`npu_hbm_used`（逐卡 HBM 已用）/`npu_davinci`（目标环境 /dev/davinci* 清单）/
`npu_logical_count`（torch_npu device_count，即 ASCEND_RT_VISIBLE_DEVICES 合法值域）。
仅当解析字段缺失或存疑时，按以下步骤人工核查（Agent 汇总"芯片清单 + 占用方"给用户确认，
不擅自判定）：

1. **全量芯片表**：`npu-smi info`——逐芯片看 HBM 占用列（新版列名 Memory-Usage/HBM），
   仅基线占用（如 ~3G/32G，驱动常驻）视为空闲；
2. **进程对照**：`npu-smi info -t proc-mem -i <id> -c <chip>`（或 info 页面 Process 区）——
   确认无其他推理进程占用该芯片；
3. **容器映射**：`docker inspect <容器> | grep -i davinci`（免 {{}} 写法）——
   目标容器实际挂载了哪些 /dev/davinci*；
4. **卡号语义**：物理号（npu-smi 显示号）≠ 逻辑号（ASCEND_RT_VISIBLE_DEVICES 取值）。
   物理→逻辑按 davinci 挂载顺序映射（如 davinci2,5 → 逻辑 0,1）；拿不准时在目标环境跑
   `python -c 'import torch_npu; print(torch_npu.npu.device_count())'` 实测值域；
5. **结论口径**：空闲芯片数 ≥ tp → 通过；占用方为其他业务 → 报告用户协调，不抢卡（铁律 7）。
   各命令参数按现场 npu-smi 版本调整（25.x 与旧版子命令不同）。

## 部署验证失败判读（对照 verify_anomaly.sh 的 JSON 明细）

| 现象 | 判读 |
|---|---|
| `v1_http_code` 非 200 | 未挂 `--middleware`（单机）或 proxy 未挂检测（PD 模式 A/B/C）；或 metrics 路径被改 |
| `v2_counters_found=false` | 端点可达但不是异常指标 → 大概率打到 vLLM 自带 `/metrics`，核对 metrics_path |
| `monitor_rate` ≤ 0 | 采样率为 0，永不检测——确认是否符合预期，调参需确认后 `POST /anomaly/config` |
| `v3_header=false`（reason=header missing） | 未命中采样（rate<1 时部分请求无请求头属正常，可多发几条）或该请求未被拦截 |
| `v4_growth` < 1 | 请求没进检测链路 → 指向/端口错误；或 `--no-request` 模式下无增长基线 |

## 检测异常的既定行为（不是故障，是设计）

- 单请求检测执行异常 → 计 `detection_errors_total` + 日志，客户端响应不受影响（已发完），
  后续请求继续检测，**不会**因此全局停用检测；
- 检测工作进程崩溃（进程池损坏）→ 自动重建进程池 + 计 error，后续请求正常；
- tk2cat 词表生成失败 → 软降级为无词表检测模式（生僻字/乱码走概率阈值路径），记 WARNING，
  服务正常启动；
- 单 token decode 失败 → 该 token 置 null，不影响其余 token 与检测。

以上均可在 `references/metrics-reference.md` 与日志中找到对应证据。
