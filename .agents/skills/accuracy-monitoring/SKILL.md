---
name: accuracy-monitoring
description: >-
  vLLM 推理精度异常监控技能。当用户需要：部署/接入/验证精度异常检测（检出生僻字/乱码/重复输出/NaN），
  单机 --middleware 或 PD 分离 proxy 集成接入，调整监控采样率 monitor_rate，通过 /anomaly/metrics
  Prometheus 指标、Web 看板、pkl 落盘查看检出结果，生成今日/昨日/最近 N 天/日期区间的
  《推理异常数据分析报告》，或排查中间件启动失败、看不到异常数据时使用。
  触发词："精度监控"、"精度异常"、"异常检测"、"异常检出"、"乱码"、"重复输出"、"生僻字"、
  "NaN"、"异常报告"、"检出报告"、"日报/周报/复盘"、"monitor_rate"、"PD 分离 精度监控"、
  "anomaly/metrics"、"下线服务"、"已停止的服务"、"停服"。
license: Apache-2.0
compatibility: >-
  随 accuracy-monitoring 仓库分发（skill/accuracy-monitoring/），亦支持 npx 独立安装
  （安装器 --repo 登记仓库根，报告能力依赖仓库）。单机模式要求目标节点 vLLM 支持
  --middleware 参数；PD 分离模式要求可运行 load_balance_proxy。异机部署依赖 ansible 通道
  （同机部署同样经 `ansible localhost --connection=local`，铁律 9）。报告在 pkl 所在节点生成，
  执行位跟随档案运行环境 runtime（容器/venv 自带依赖时在运行环境内执行；需 numpy、PyYAML，
  tokenizer 解码需 transformers，缺失时报告仍生成分布，明细无法解码）；
  控制节点仅跑 merge_reports.py 合并（纯 stdlib）。
metadata:
  role: monitoring-operations
---

# vLLM 推理精度异常监控

## 概述

> **一句话定位**：指导 Agent 完成 vLLM 推理精度异常检测（生僻字/乱码/重复输出/NaN）在单机与
> PD 分离两种形态下的接入与验证，通过 Prometheus 指标 / Web 看板 / pkl 落盘三个通道检视检出
> 结果，并对任意时间窗生成多实例《推理异常数据分析报告》。

**背景事实**（回答用户问题时的公共前提）：

- 检测组件对客户端**透明**：拦截推理请求、强制采集 logprobs 与 token_id、后台检测，
  不影响客户端响应；仅覆盖 `/v1/chat/completions` 与 `/v1/completions`，流式与非流式、
  多候选 `n>1` 均支持；
- 四类异常：1=生僻字、2=乱码、3=重复、4=NaN（logprobs 出现 nan/inf）；
- 两种部署形态：**单机**（`--middleware` 插件随 vllm serve 进程）与 **PD 分离**（检测上移至
  load_balance_proxy，全局唯一检测点，P/D 节点启动命令保持纯净）；
- 三个结果通道：`GET /anomaly/metrics`（Prometheus 文本，机器读）、Web 看板
  （`webui/`，聚合多实例 + 阈值告警）、pkl 落盘（`VLLM_ANOMALY_SAVE_PATH`，异常全量现场）；
- 唯一可热更的运行时配置：监控采样率（`GET/POST /anomaly/config`），不持久化，重启回退 env。

**心智模型**：`档案（身份/参数起点） → 接入（单机/PD） → 验证（证据闭环） → 检视（三通道决策） → 报告（任意时间窗）`

**执行总序（一切任务共享的主干顺序；W1~W5 是它在各场景的展开。每个工作流步骤都标注了
对应的总序映射，迷路时回看本表定位所处阶段与下一步）**：

| 步 | 动作 | 入口条件 | 出口判据 |
|---|---|---|---|
| S0 查档案 | 检索 instances.yaml（落点见路径约定），命中实例按 running/stopped 分列列参数 | **任何任务第一步**，先于提问/探测/通道准备 | 命中→S2；缺失→S1 |
| S1 问用户 | 仅档案缺失字段一次问全；答案回写档案 | S0 有缺失 | 必填清单齐 |
| S2 定点验证 | probe 实测档案/用户给的值（路径/端口/卡/中间件/包） | S0 或 S1 完成 | 事实清单齐（铁律 2） |
| S3 变更同意 | 重启/落盘/占卡/停服逐项征得同意（铁律 6/7） | 涉及变更时 | 用户明确确认 |
| S4 执行 | deploy/stop/proxy 经 ansible 投递 | S2+S3 过 | 已下发 + 日志路径 + 实际端口 |
| S5 验证 | verify 轮询至 V1-V4 证据闭环 | S4 完成 | rc=0；无证据不得称生效（铁律 1） |
| S6 收尾 | 汇报 + webui 登记 + 档案回写 | S5 过 | 收尾三件套齐（档案=下次会话唯一起点） |

| 编号 | 能力 | 用户意图示例 | 入口 |
|---|---|---|---|
| C1 | 部署接入（单机） | "给这个 vLLM 服务加上精度监控" | W1 |
| C2 | 部署接入（PD 分离） | "PD 分离场景怎么监控精度" | W2 |
| C3 | 部署验证 | "确认一下检测真的在跑" | 验证清单 V1-V4 |
| C4 | 运行态操作与三通道检视 | "看看现在有没有异常""采样率调到 0.3" | W3 |
| C5 | 灵活报告生成 | "出一份最近 7 天的异常报告" | W4 |

## 何时用 / 何时不用

**用**：

- 给 vLLM 推理服务接入精度异常监控（单机或 PD 分离，服务未启动或在线运行均可）；
- 已接入，想确认检测真的在生效；
- 值班巡检/收到告警后，查看当前与历史的异常检出情况；
- 生成今日/最近几天/任意区间的异常检出报告；
- 动态调整监控采样率（压测降采样、复现拉满）；
- 查看某次异常的完整现场（prompt/token/logprobs/输出文本）；
- 查询某服务器上已下线/停止的监控实例（档案检索），复用其参数重新拉起。

**不用**（如实告知超出本技能范围，不替换、不硬做）：

| 场景 | 说明 |
|---|---|
| 主动制造故障测试系统韧性（网络延迟/CPU 过载/进程 kill…） | 故障注入/混沌演练，需使用相应注入工具，非本技能 |
| 深挖"什么输入触发了异常"（触发规律统计、假设验证） | 本技能只做监控运维闭环，不做触发规律深挖 |
| 模型通用能力/准确率评测（benchmark 打分） | 评测体系，非本技能 |
| 检测算法阈值调优 | 修改 `configs/detector.yaml` 属算法调优流程，非本技能 |

## 铁律（必须遵守）

1. **没有证据不得声称"检测已生效"**。生效唯一判据 = 验证清单 V1-V4 通过；`vllm serve` 启动
   成功、进程存活、curl 不报错都**不是**生效证据。
2. **动态事实以实测为准，不从记忆反推**。metrics 路径、端口、monitor_rate 当前值——先 curl /
   查 env 再回答；本文件不写死会随部署变化的值。服务档案（`configs/instances.yaml`）中的
   **动态状态**同样不可直接采信，执行前实测。
3. **落盘未开启就如实说**。未设 `VLLM_ANOMALY_SAVE_PATH` → 无 pkl → 明细报告无法生成。
   如实告知 + 给出开启步骤（设 env + 重启生效），绝不伪造明细。
4. **报告结论必须可回查**。每条明细标注实例名 + pkl 异常编号；用户可回原始文件核对。
5. **检出事实以 pkl 为准**。离线重跑（阈值 `configs/detector.yaml` + 实例 tokenizer 词表）仅用于
   定位与研判，不改变报告计数；重跑与线上不一致时如实按线上 ill_type 展示，不阻塞、不篡改。
6. **在线服务接入必须先征得同意**。`--middleware` 为启动期参数、无法热挂载——对运行中的服务
   接入 = 重启级操作。重启前必须完成：① 重启告知（「需先关闭服务重新拉起，请确认是否现在添加」，
   生产服务可能不能随意关闭）；② 一并询问是否开启 pkl 落盘及路径（启动期配置，与重启合并为一次
   变更，避免二次停机）；③ 变更清单（新命令 + env + 中断时长）获确认后再执行。未获同意不得
   重启/杀进程/改启动配置；其他影响运行的操作同样先确认再执行。
7. **资源不足不抢卡，卡号语义不可凭感觉**。启动/重启前按启动脚本解析加速卡需求（如 tp=4 → 4 卡），
   核验空闲卡数；不足时如实告知并给替代方案（等待释放/协调退出/换机器/调 tp），**不得关闭正在
   运行的进程腾卡**；停服重启须确认原卡片已释放再拉起，被抢占/残留时不得强杀，回到征求同意环节。
   **NPU 卡号两套编号，严禁混用**：`npu-smi info` 显示的是【物理卡号】（如 2/5），
   `ASCEND_RT_VISIBLE_DEVICES` 要的是【逻辑卡号】（torch_npu 枚举序 0..N-1；容器按物理号挂载
   davinci2/5 时逻辑号是 0/1）——物理号填入该 env 会在 aclInit 报 107001 "Invalid device ID"，
   引擎启动即崩（真实事故）。指定卡号前必须先跑 probe 取 `npu_logical_count`/`npu_davinci`/
   空闲卡映射；deploy 对非法值直接拒绝（rc=3），取不到值域时强告警，此时不得拍脑袋填值。
8. **单实例失败不阻塞**。报告生成中某实例 pkl 缺失/解析失败 → 保留该实例章节并写明原因与补救
   动作，其余实例正常出报告；汇报时明确列出失败实例。
9. **仅经 ansible 连接被测服务器**。对被测服务器的 shell 级操作（进程/卡/文件/服务）一律经
   ansible 投递执行——禁止 ssh 直连操作、禁止默认本机操作；**控制节点自身有没有卡/进程不影响
   本通道约定**（同机场景同样走 `ansible localhost --connection=local`，审计路径一致），且
   不得假设"管理节点无 vllm 无加速卡"——那是经验假设，以 probe 实测为准（真实反例：某控制
   节点自身有空闲 NPU 与 vllm 安装，见档案 meta note）。**安装归属**：系统级组件（ssh client、
   apt/yum 包）由用户安装——Agent 检测缺失、给命令、复验；pipx 已存在时 Agent 可
   `pipx install ansible` 一次；sshpass 属低风险工具 Agent 可自装；凭证永远用户执行（铁律 10）。
   **网络失败即停（适用于一切网络依赖操作）**：pipx/pip/apt/yum 安装、sshpass 安装、
   ansible 连接、包/词表下载——任一因网络失败（超时/不可达/解析失败/源 403-404）立即停止并
   报告错误原文，交用户处理；禁止换源、配置代理、反复重试、安装隧道/VPN 工具——网络策略是
   用户环境决策，Agent 永不代做。远端命令失败（127/权限拒绝/连接失败）≠ 能力不存在，先核对
   目标主机与回传 rc 再下结论。HTTP 类探测（metrics/config/模型端点）可由任意可达节点直连。
10. **凭证纪律（密码可用，必须引用化）**。凭证本体（密码/密钥/vault 口令）不得进入对话上下文、
    服务档案、报告与命令行参数（会进 ps 与 shell history）。三条合法形态：
    ① SSH 密钥免密（首选）；② **密码受控通道**——用户在管理节点自建密码文件
    （`umask 077 && echo '密码' > ~/.ansible_cpass`），Agent 仅获得文件路径（引用，可登记），
    调用时经 `--connection-password-file <路径>` 使用，密码本体绝不入对话/档案/命令行；
    ③ ansible-vault（口令文件走环境变量）。细则见 references/remote-execution.md「凭证纪律」。
11. **先档案、后提问、再探测，禁止漫游**。拉起/接入/报告所需信息（模型路径、端口、运行环境、
    启动方式、凭证引用）按固定顺序获取：① **查全局服务档案**（落点见路径约定；本机只读、
    零成本，命中即复用静态身份参数，不重复问用户）；② 档案缺失的字段**才向用户提问**，
    答案回写档案；③ 仅当用户明确说"不知道/帮我看看有哪些"时，才跑**轻量服务发现**
    （`probe_env.sh --discovery`：只列 8xxx 端口的运行中 vllm 服务，秒级返回）辅助选择。
    查档案不是禁令对象——禁的是对目标机的无授权扫描：**禁止**漫游目标机目录、翻找模型文件、
    逐个检查容器/conda/虚拟环境——那类信息只来自档案/用户或用户确认后的定点验证
    （probe 验证档案或用户给出的值），绝不主动扫描。
    **middleware 支持的判定判据** = `vllm serve --help 2>&1 | grep -- '--middleware'`；
    **未命中时必须补查 `vllm serve --help=all 2>&1 | grep -- '--middleware'`**（部分构建
    默认 help 不展示该参数、仅全量 help 可见，如 vllm-ascend v0.18.0）：
    任一命中即支持，两者皆未命中才判不支持（贴 help 原文收尾）——**禁止**翻查 vllm 源码、
    CLI 注册入口、参数名版本考证来"解释为什么"。

## 路径约定

- `<SKILL_DIR>` = 本 SKILL.md 所在目录；**仓库根解析顺序**：
  ① 环境变量 `ACCURACY_MONITOR_REPO` →
  ② `<SKILL_DIR>/../..`（随仓库安装时，判定依据：其下存在 `tools/anomaly_report.py`）→
  ③ `<SKILL_DIR>/.repo` 文件内容（npx 独立安装时由安装器 `--repo` 写入）→
  ④ **直接询问用户仓库路径**（与铁律 11 同型：档案/env/登记皆无果后才问）——用户给的副本需**校验完整性**：
     `tools/anomaly_report.py`、`configs/detector.yaml`、`anomaly_middleware/` 包存在；
     缺失 → 报告"该副本布局与技能预期不一致"，列出依赖清单，不擅自改用；
  皆无 → 报告生成/PD 词表工具不可用，如实告知用户需检出仓库并 `pip install -e .`；
- 仓库内脚本：`<仓库根>/tools/anomaly_report.py`（报告生成——在 pkl 所在节点的运行环境内执行
  （执行位跟随档案 runtime），工具代码查缺顺序：节点侧检出根 repo_checkout → 控制节点副本
  补投缺失文件 → 双侧皆缺请用户检出仓库，见 W4.4 与 report-tool.md「执行位置」）+
  `<仓库根>/tools/merge_reports.py`（合并——控制节点本机执行）；
- 投递脚本：`<SKILL_DIR>/scripts/probe_env.sh`、`deploy_monitor.sh`、`verify_anomaly.sh`
  （自包含，一律经 `ansible -m script` 免预装投递执行）；
- **全局服务档案落点**：仓库可用 → `<仓库根>/configs/instances.yaml`；
  **repo-less（仓库解析失败）→ `<SKILL_DIR>/.archive/instances.yaml`**（目录自动创建）；
  档案是**身份/参数缓存**不是**状态缓存**——静态身份信息可信复用，动态状态执行前实测；
  instances[] 可登记**连接引用字段**（铁律 10 允许的"引用"）：`ssh_user`、
  `connection_password_file`、`key_path`；凭证本体仍绝不入档案。
  **档案必须包含控制面状态段 `webui:`**（status/port/code_path/tunnels[]/registered[]——
  webui 在跑、隧道本地端口、已登记实例 URL），部署收尾时回写；档案缺 webui 段 ≠ webui 没在跑
  （可能是历史会话漏回写），仍需先 `webui_control.sh status` 实测再处置。
  verified 字段结构化：`{result: <V1-V4 pass>, at: '<ISO 时间 UTC>'}`，不用自由文本。
  **停服双轨约定**：instances.yaml 条目**保留** + 结构化 `stopped` 标记
  （`{result: <停服结果>, at: '<ISO 时间 UTC>', note: <原因/影响>}`——身份参数与连接引用是
  重启复用的第一数据源，删除不可逆）；webui.yaml 登记项**默认注销** + 对应隧道拆除（展示层
  只留活实例，防灰色噪音累积）；重新拉起后删 stopped 标记、重新 register。

## 命令骨架

```bash
# —— 探测（只读，幂等；一律经 ansible 投递）——
# 服务发现（轻量，秒级）：仅列 8xxx 端口运行中 vllm 服务——仅在用户说"不知道有哪些"时使用
ansible <目标主机> -i <inventory> -m script -a "<SKILL_DIR>/scripts/probe_env.sh --discovery" -b
# 全量探测（部署前检查：middleware 支持/包安装/硬件卡数/落盘路径）——针对用户给出的值做定点验证
ansible <目标主机> -i <inventory> -m script -a "<SKILL_DIR>/scripts/probe_env.sh --tp 4 --save-path /data/anomaly" -b
# 容器目标：--docker 进容器内检查（venv 场景加 --vllm-bin 绝对路径）
ansible <目标主机> -i <inventory> -m script -a "<SKILL_DIR>/scripts/probe_env.sh --docker <容器名>" -b

# —— 停服（变更执行，同意后投递；换端口/换参数重启场景用；精准目标批，绝不扩大击杀范围）——
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/stop_service.sh [--docker <容器名>] --yes" -b
# rc: 0=全部退出  1=有残留（交人工，不自动扩杀）  3=目标无 vllm 进程  2=用法/目标错误
# 拉起前必须经 probe/npu-smi 实测卡片已释放（铁律 7）
# 停服收尾双轨：① instances.yaml 条目保留 + 结构化 stopped 标记（重启复用数据源）；
# ② webui.yaml 登记项默认注销（register_instance.sh remove）+ 隧道拆除（tunnel --stop）

# —— 部署（变更执行，确认后才投递；拉起后立即返回"已下发"）——
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/deploy_monitor.sh --model <模型> --tp 4 --save-path /data/anomaly" -b
# 带值 vllm 参数直接透传（未知 --flag VALUE 自动成对透传；歧义场景用 --extra-args "..." 整串）
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/deploy_monitor.sh --model <模型> --port 8010 --tp 1 --served-model-name Qwen3-0.6B --save-path /data/anomaly" -b
# 端口顺位探测：请求端口（或默认 8000）被占用 → 自动 +1 重试（至多 +20）直至空闲，
# 成功拉起后回显实际端口（JSON requested_port/port 两字段）——汇报用户时必须用实际端口
# 指定加速卡/附加环境变量：--env KEY=VALUE（可重复，如 --env ASCEND_RT_VISIBLE_DEVICES=1）；
# 多实例（同容器/同机第二个监控实例）加 --force 越过防重，端口/卡不冲突由调用方自证
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/deploy_monitor.sh --docker <容器名> --model <模型> --port <端口> --save-path <容器内路径>" -b
# 容器目标：middleware 须已装入容器（deploy 仅实测 import，缺失即拒绝并给安装指引）；
# --config-file 为宿主机上的 detector.yaml（wheel/exec 安装缺 configs 时 docker cp 补齐）
# launch-script 模式：脚本内必须自带 --middleware（deploy 只注入 env 不改写脚本 CLI 参数），
# 缺失默认拒绝（rc=3）；脚本内 --port 被占用时自动改写脚本副本顺位端口后拉起

# —— 验证（活性轮询：就绪即返/致命即败/停滞即停/窗口耗尽自动续投）——
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/verify_anomaly.sh --port <端口> --wait 300 --interval 15 --log <deploy返回的日志>" -b
# 容器目标：HTTP 探测与日志读取均经 docker exec（--log 为容器内日志路径）
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/verify_anomaly.sh --docker <容器名> --port <端口> --wait 300 --interval 15 --log <deploy返回的容器内日志>" -b
# rc: 0=就绪且 V1-V4 过  1=窗口内未就绪（脚本已对全量日志做过致命扫描；Agent 自动续投下一轮，
#     不问用户；累计 ≤40 分钟上限；未传 --log 时 Agent 续投前必须自行 grep 日志 ERROR/Traceback）
#     3=启动失败（附日志错误原文，立即给结论）  4=加载停滞（附进度末行）  2=用法/目标错误

# —— webui 生命周期（控制节点本机执行，不走 ansible；策略：仅控制节点、首选 9092 顺位探测、bind 0.0.0.0）——
sh <SKILL_DIR>/scripts/webui_control.sh status                      # 四态：端口身份/代码/依赖
sh <SKILL_DIR>/scripts/webui_control.sh start --code-path <仓库根>   # 9092 被无关进程占用 → 顺位 +1（至多 +20）；
                                                                    # 成功后回显实际端口；缺依赖 rc=6 → Agent pip 装一次
sh <SKILL_DIR>/scripts/webui_control.sh tunnel --ssh-host <被测节点IP> --ssh-port 2123 \
   --target-port <实例端口> --pass-file ~/.ansible_cpass              # 实例对控制节点不可达时先建隧道；
                                                                    # 端到端验证 /anomaly/metrics=200
sh <SKILL_DIR>/scripts/webui_control.sh register --config <webui.yaml> \
   --name <实例名> --url http://127.0.0.1:<隧道本地端口>               # 先验可达性，不可达默认拒绝

# —— webui 实例登记（webui.yaml 所在节点经 ansible 投递执行；instances 段热重载即生效）——
ansible <目标主机> -m script -a "<SKILL_DIR>/scripts/register_instance.sh [--docker <容器名>] --config <webui.yaml路径> add --name <实例名> --url http://127.0.0.1:<端口>" -b

# —— 部署验证 HTTP 四条（也可由可达节点直连）——
curl -s -o /dev/null -w "%{http_code}" http://<host>:<port>/anomaly/metrics        # V1: 200
curl -s http://<host>:<port>/anomaly/metrics                                       # V2: 计数与采样率
curl -s -D - -o /dev/null -X POST http://<host>:<port>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'   # V3: 请求头
curl -s http://<host>:<port>/anomaly/metrics | grep vllm_anomaly_requests_total    # V4: 增长≥1

# —— 运行态调参（唯一热更项）——
curl http://<host>:<port>/anomaly/config
curl -X POST http://<host>:<port>/anomaly/config -H "Content-Type: application/json" -d '{"monitor_rate": 0.2}'

# —— PD 模式 C 启动器（在 proxy 所在节点执行，经 ansible）——
python <仓库根>/tools/run_proxy_with_anomaly.py <proxy脚本> [proxy原参数] --anomaly-save-path <目录>

# —— 报告（标准 = 节点侧生成 + 控制节点合并；相对时间先在控制节点换算为绝对窗口 --from/--to 下发）——
# 节点侧：先按档案 runtime 定点验证执行环境（pkl/依赖/工具代码，见 W4.3）→ 渲染每节点最小
#         instances.yaml → 在 pkl 所在节点的运行环境内执行 anomaly_report.py（检出根已有全套
#         工具代码只投最小 yaml；缺/旧才从控制节点副本补投；pkl 原地零传输）→ 产出
#         html/md/stats.json fetch 回（容器内产出先 docker cp 出宿主机）
ansible <目标主机> -i <inventory> -m script -a "<工具包>/anomaly_report.py --config <每节点最小yaml> --from <X> --to <Y>"
# 控制节点（本机执行，不走 ansible）：merge_reports.py 合并各节点报告
python <仓库根>/tools/merge_reports.py --output <合并报告.html> <报告1.html> ...
# 遗留备选路径（仅单机自查/离线深挖）：pkl 同步到控制节点后本机直跑
# python <仓库根>/tools/anomaly_report.py --config <yaml> --days 7   # 单日 --date / 区间 --from --to
```

## 标准工作流

```
W1 单机接入（步骤编号 W1.1..W1.11，按序执行；每步出口判据未满足不得进入下一步；括号内为总序映射）：
 - [ ] W1.1 **查档案（= 总序 S0；铁律 11 第一步，先于一切提问/探测/通道准备）**：
        按 host 过滤 instances.yaml，该机实例分 running/stopped 两列展示；running 状态顺手对
        /anomaly/metrics 秒级 HTTP 复核再标注（档案状态是缓存，铁律 2）。有历史实例 → 列出
        参数（实例名/端口/模型/卡号/落盘路径/停止时间）让用户选：**复用**（running 直接用；
        stopped 按原参数拉起）还是**另拉新实例**。复用确认后仍必须 probe 实测（卡空闲/端口
        占用/middleware 在装——停止 ≠ 资源还空着）→ 复用参数走 W1.9 deploy/verify → W1.10
        重新 register webui → W1.11 档案删 stopped 标记。多命中摆出来让用户挑，不自动选；
        档案无命中或用户要全新参数 → 落到 W1.3
 - [ ] W1.2 通道准备：连接被测服务器仅经 ansible（铁律 9）；未安装 → pipx install ansible 尝试一次，
        仍失败 → 请用户安装后继续，停止尝试
 - [ ] W1.3 **问用户（= 总序 S1；铁律 11 第二步，仅 W1.1 档案缺失的字段）**：一次性问全必填清单——
        目标主机、模型路径、**端口**（缺省 8000）、**pkl 落盘路径**（启动期配置，缺省建议开启）、
        运行环境（容器/虚拟环境）、启动命令（tp/served-model-name/其他 vllm 参数）、**多实例意图**
        （同机/同容器是否已有监控实例）——用户能提供就跳过一切探测；用户说"不知道/帮我看看
        有哪些"时才跑轻量服务发现（probe_env.sh --discovery：仅列 8xxx 端口运行中 vllm 服务，
        秒级）辅助选择；禁止漫游目录、翻找模型文件、逐个检查容器/conda；**获得的信息回写档案**
 - [ ] W1.4 信息验证（= 总序 S2）：probe（定点验证**档案或用户给出的值**：**--model 路径存在性 /
        --port 占用实测** / tp 需求 vs 空闲卡）+ 前置检查（middleware 支持/anomaly_middleware 安装）
        ——middleware 缺失则给用户安装命令（`cd /path/to/accuracy-monitoring/ && pip install -e .`，
        容器先 exec -it 进入），**用户在环境内装好后 probe 复验，Agent 不代装**
 - [ ] W1.5 **pkl 落盘决策点（probe 报 save_path_file_exists=yes 或 dir_pkls 非空时必经）**：
        目标 pkl 已存在 = 用户决策，Agent 不得默认任何处置——摆四选项让用户选：
        **沿用追加**（历史数据保留，报告按时间窗区分）/ **改名新文件起跑**（旧文件原地保留）/
        **删除旧文件**（不可逆，须用户对文件大小/mtime 明确确认）/ **换路径**。旧 pkl 含历史
        检出现场，删除前必须复述大小/mtime 与影响范围；文件夹模式注意实际文件名=模型名.pkl，
        可能命中目录内既有同名文件；复用档案参数拉起已停实例时此决策点几乎必触发（旧 pkl 大概率还在）
 - [ ] W1.6 **报告能力检查（通道准备时一并判定）**：仓库解析（ACCURACY_MONITOR_REPO /
        随仓布局 / .repo 登记 / 用户口头给的路径）        全部失败 → repo-less 模式：如实告知用户
        "控制节点无仓库副本；明细报告是否可用取决于节点侧检出根（档案 repo_checkout /
        runtime 内 import anomaly_middleware 定位，W4.3 定点验证），指标/看板/pkl 落盘不受影响"；
        日后出报告需先解析仓库路径（检出仓库并 pip install -e .，走第 ④ 步）——标准流程报告
        在 pkl 所在节点的运行环境内生成，pkl 不需回传控制节点（见 report-tool.md「执行位置」）。
        **仓库路径解析成功 → 立即登记进档案**（下次会话免重判 repo-less——真实教训：仓库
        已在 <repo> 下检出，会话仍按 repo-less 汇报，因未做第 ④ 步询问用户）
 - [ ] W1.7 配 env（落盘询问）
 - [ ] W1.8 在线已挂 middleware → 直接转 W1.9 验证（跳过部署）；在线未挂 → 重启告知（征得同意，
        = 总序 S3）→ 落盘询问 → 变更清单确认 → 重启（stop_service.sh 停服 → probe 实测确认卡片释放）
 - [ ] W1.9 **二次确认「已准备好，是否现在拉起」→ 部署+验证（= 总序 S3 出口 → S4 → S5）**：
        deploy_monitor.sh 后台拉起（记下返回的日志路径与**实际端口**——请求端口被占用时脚本自动
        顺位 +1，须以 JSON 的 port 字段为准并告知用户）→ verify 活性轮询：`--wait 300
        --interval 15 --log <日志路径> --port <实际端口>`（容器加 `--docker <容器名>`，日志为
        容器内路径）；rc=1 自动续投下一轮（不问用户，累计等待 ≤40 分钟上限）。**续投纪律**：
        verify 窗口耗尽时已对全量日志做过致命扫描（rc=1 = 全量无致命特征）；但若调用时未提供
        `--log`，Agent 续投前**必须**自行 grep 一次日志 ERROR/Traceback，命中即按 rc=3 处理
        （贴错误原文 + 根因结论，不得进入下一轮等待）；连续 2 轮 rc=1 → 先实测进程存活与端口
        监听再续投；rc=3/4 立即贴日志错误原文/进度末行给结论（rc=3 输出已附日志末 25 行，
        Agent 过目一眼排除可重试的 RuntimeError 误报再定性"启动失败"）
 - [ ] W1.10 **webui（仅控制节点，首选端口 9092，bind 0.0.0.0）——【部署完成硬门槛，不得整体跳过】**：
        先查档案 webui 段：**已在跑 → 复用（不重启不换端口），直接走本实例 register**；未记录 →
        webui_control.sh status 定四态 → 9092 空闲：start（代码取优先级：档案/用户给定路径 →
        控制节点无代码则经 ansible 从被测节点**拷回最小集** webui/ + configs/ + tools/ +
        anomaly_middleware/（Python 源码集，KB~百 KB 级，绝不整仓——仓库可能含模型/构建产物；
        **含 tools/ + anomaly_middleware/ 是 W4 报告的来源前提**：控制节点副本完整性以被测节点
        检出为源，被测节点当年就没有检出则双侧皆缺，W4 只能请用户检出仓库）——容器先 tar +
        docker cp 出宿主机再 fetch，见 webui-guide.md；缺依赖 rc=6 → Agent pip 安装**一次**，
        失败交用户）；9092 已被本 webui 占用：直接注册；被无关服务占用：start 自动顺位 +1
        （至多 +20）拉起并**告知用户实际端口** → 实例端口对控制节点不可达（安全组）：先 tunnel
        建隧道（本地端口默认 1<目标端口>，如 8002→18002），再以隧道地址 register（register 默认
        拒绝不可达 URL；对隧道地址 register 前 register 自带可达性校验）→ **register 成功与否
        必须写进部署汇报**（实例名 + URL）→ 告知访问路径（用户远程访问控制节点：
        `ssh -L <webui实际端口>:localhost:<webui实际端口> <控制节点>`）→ 被测节点遗留 webui
        建议关闭（**需用户确认**，避免双看板；趋势数据无法迁移，自启动起累积）
 - [ ] W1.11 **收尾三件套（= 总序 S6；部署完成的 exit criteria，缺一不算完成，汇报中逐项打勾）**：
        ① verify V1-V4 全过；② webui 已登记新实例（或 webui 不可用的原因记录在案并告知用户）；
        ③ 全局档案回写：新实例条目（含**实际端口**、卡号及映射依据、落盘路径、部署日志路径、
        **runtime 运行环境**（docker:<容器名>/venv:<解释器路径>/conda:<env名>/bare）与
        **repo_checkout 节点侧检出根**（middleware 安装源目录——W4 执行位与工具代码查缺依据））+
        webui 段（status/port/code_path/tunnels/registered）+ 结构化 verified 字段 +
        仓库根路径（解析成功时登记，免下次会话重判 repo-less）——**档案是下次会话的唯一起点，
        控制面状态（webui/隧道/仓库路径）不回写 = 下次从头重查**
W2 PD 接入：
 - [ ] W2.1 查档案（= 总序 S0，铁律 11）：proxy 节点/词表路径/端口/集成模式/SAVE_PATH 命中即复用，
       缺失才问（= S1，答案回写档案）
 - [ ] W2.2 预生成词表（gen_token_category.py，仅需模型目录）→ 选集成模式（默认 C）→ 配
       VLLM_ANOMALY_TOKEN2CATEGORY → 经 ansible 拉起 proxy（CPU 服务，无卡校验）
 - [ ] W2.3 验证（= 总序 S5，对 proxy 跑 V1-V4）
 - [ ] W2.4 webui 注册/拉起 → 告知路径 → 档案回写（= S6：proxy 条目 + webui 段 + verified）
W3 检视（决策表见 references/metrics-reference.md 与 webui-guide.md）：
 - [ ] W3.1 查档案定实例（= 总序 S0）：名称/端口/pkl 路径/SAVE_PATH 开启日/monitor_rate 历史值，
       缺失才问（回写档案）；动态状态执行前实测（铁律 2）
 - [ ] W3.2 密度/趋势类问题：必须读数据给结论（webui /api/trends → pkl 分桶 → PromQL），不推诿看板；
       结论口径：密度≠计数、窗口等长、monitor_rate 变更陷阱、数据源存活窗口声明
 - [ ] W3.3 异常现场回查：按 pkl-data-contract.md 读 pkl 序号（截断脱敏展示）
W4 报告（标准 = 节点侧生成 + 控制节点合并，见 report-tool.md）：
 - [ ] W4.1 查全局服务档案（= 总序 S0）→ 缺失才问用户（= S1，答案回写档案）；解析目标实例 →
       按节点分组
 - [ ] W4.2 相对时间在控制节点换算为**绝对窗口 --from/--to** 下发（免疫节点时钟偏差）
 - [ ] W4.3 **执行环境定点验证（= 总序 S2；执行位 = 验证位 = 档案 runtime，不限容器）**：
       档案记 docker:<容器名> → 进容器验证；记 venv/conda → 用该解释器验证；bare → 宿主机；
       档案缺 runtime 记录 → 先补档（问用户/实测）再验证，不猜。在运行环境内定点验证三项：
       ① pkl/tokenizer 可见性（挂载目录宿主机直达的说明见 report-tool.md 前置条件 3）；
       ② python 依赖 numpy/PyYAML/transformers（transformers 缺 → 分布表正常、明细无解码的
       降级见 report-tool.md 前置条件 1）；③ 工具代码：`import anomaly_middleware` 定位检出根
       （能拉起监控 ⇒ 运行环境内必有 anomaly_middleware；editable 安装 ⇒ 检出根多半在）→
       验 tools/anomaly_report.py + configs/detector.yaml **存在性与版本一致性**（旧检出可能
       缺新报告工具；非 editable 安装 site-packages 无 tools/）
 - [ ] W4.4 渲染每节点最小 instances.yaml → 在 pkl 所在节点的运行环境内执行 anomaly_report.py
       （**查缺顺序**：检出根已有全套工具代码 → 只投最小 yaml；旧版/缺文件 → 从控制节点副本
       补投缺失文件；**双侧皆缺 → 停止，请用户检出仓库**，不硬做）；节点侧产出
       <报告>.html + .stats.json → fetch 回（容器内产出先 docker cp 出宿主机）
 - [ ] W4.5 merge_reports.py 合并：总览一表 N 行（列名对齐取值，不重算求和）+ 章节按埋点拼接
       + 失败实例声明（--error）；stats_version 不兼容明确报错
 - [ ] W4.6 汇报：总览数字/各实例类型分布与类型变化/值得注意的趋势 + 失败实例 + 报告路径与阅读指引
W5 排障：
 - [ ] W5.1 查档案取实例身份（= 总序 S0）：端口/容器/部署日志路径/middleware 形态/最近 verified
       与 stopped 记录；动态状态执行前实测（铁律 2）
 - [ ] W5.2 按 references/troubleshooting.md 逐项判读
```

**W4 端到端示例**（"请帮我总结最近一个月的检出情况，并形成报告"）：

```
① 意图解析：--days 30（换算起止日期明确告知；用户指自然月则改 --from --to）。
   ⚠️ 统计必须严格限定用户指定的时间点——禁止用 --all 或扩大窗口回应"最近 N 天"类请求
   （--all 仅当用户明确要求全部数据时使用）
② 前置探测：查服务档案 → 命中直接取实例清单与 pkl 路径，缺失才问（回写）；核 pkl 覆盖窗口；
   按档案 runtime 定点验证执行环境（W4.3 三项：pkl/依赖/工具代码）
③ 数据窗口对齐声明：实际统计窗口 = 请求窗口 ∩ pkl 实际覆盖（SAVE_PATH 开启日起才有数据）
④ 参数决策：记录量大 → 先 --no-detector 出分布，要明细再收窄区间/类型重跑
⑤ 生成（节点侧运行环境内并行 + 控制节点合并）：换算绝对窗口 --from/--to → 在各节点 runtime
   内执行 anomaly_report.py（检出根有全套只投最小 yaml，缺/旧才补投）→ fetch html+stats.json →
   merge_reports.py 合并；实例失败不阻塞
⑥ 总结输出：总览数字/各实例类型分布与类型变化/值得注意的趋势（变密变稀）/失败实例
   + 口径提醒（pkl 覆盖是否完整、monitor_rate 期间变更影响）→ 报告路径 + 阅读指引
⑦ 后续建议：某类激增 → W5/W3；触发规律深挖 → 超出本技能范围，如实说明
```

## 常见错误

- **把"启动没报错"当生效** → 必须 V1-V4 证据；
- **metrics 404 就说组件坏了** → 可能 `VLLM_ANOMALY_METRICS_PATH` 被改过，先查 env；
- **没开落盘还想出明细报告** → 如实说明并给开启步骤，不伪造；
- **`--days` 与 `--from/--to` 混用** → 时间窗参数互斥，报错退出；
- **把"请看 webui"当答案推诿** → 密度类问题必须读数据给结论，看板路径只作末尾复核指引；
- **把检出计数增长当异常变密** → 未剔除流量变化/monitor_rate 变更影响，密度=Δ检出/Δ请求数；
- **在线服务未征得同意就重启** → 违反铁律 6；
- **资源不足杀进程腾卡** → 违反铁律 7；
- **管理节点本机执行远端操作** → 违反铁律 9（127/失败 ≠ 能力不存在）；
- **跳过档案直接问用户/直接探测** → 违反铁律 11 顺序（档案 → 问 → discovery）：档案命中仍
  重复索要参数 = 浪费用户输入，且与"档案是下次会话的唯一起点"设计冲突（真实教训：
  同主机实例已在档案登记，会话仍向用户重问全部参数）；
- **擅自漫游目标机目录/翻找模型文件/逐个查容器** → 违反铁律 11（先档案、后提问、再探测）；
  定点验证仅针对档案/用户给出的值；
- **grep 未命中就去翻 vllm 源码/CLI 注册入口考证参数名** → 违反铁律 11；
  结论只有两个：任一 help 命中=支持，`--help` 与 `--help=all` 皆未命中=不支持（贴 help 原文），
  然后收尾——**只查默认 help 就判"不支持"是漏检**（部分构建仅 --help=all 展示该参数）；
- **ansible 装不上就反复换姿势安装（apt/docker/源码…）** → pipx 尝试一次，失败交还用户（铁律 9）；
- **系统级组件（ssh client/apt 包）自己动手装** → 违反安装归属矩阵：用户装，Agent 出清单并复验；
- **网络不可达/需要代理时反复重试安装或连接** → 违反铁律 9"网络失败即停"：
  一次失败贴错误原文交用户，禁止换源/配代理/重试循环；
- **把服务器密码直接粘贴进对话或写入服务档案** → 凭证本体不入对话/档案/命令行（铁律 10）；
  用密码时走受控通道：用户自建密码文件（Agent 给命令模板、用户自己执行），Agent 仅引用路径；
- **采信档案里的动态状态** → 违反铁律 2，执行前实测；
- **PD 三种集成模式叠加** → 双重采样/注入，检测结果失真；启动器检测到已挂钩会拒绝启动。
- **手写 kill/pkill 停服** → 高危且易误伤同机其他 vllm 实例；一律用 stop_service.sh
  （精准目标批 + 宽限期 + 仅同批 KILL + 卡释放复核）；
- **把 probe 的 pkg=missing 当唯一事实** → probe 已用 vllm shebang 解释器复测并输出
  pkg_proc_mismatch 分歧标志；进程在跑 ≠ 新拉起能 import，重启前必须补装或复现原加载方式；
- **deploy 透传参数拼写错误静默失败** → 未知 `--flag` 不再报错而是透传给 vllm serve，
  拼错的部署自有选项会在启动期报 unrecognized arguments，由 verify rc=3 捕获——
  透传前自查拼写，verify rc=3 时先核对 deploy 输出的 cmd 原文。
- **同容器/同机第二实例被 rc=4 拒绝就手写 docker exec 绕过** → 防重按容器/机器粒度属设计局限，
  多实例场景用 deploy `--force` 越过（端口/卡不冲突由调用方自证：probe --port 实测 + 卡况核实），
  保持 env 注入/日志规范/JSON 返回与脚本一致；
- **launch-script 漏 `--middleware`** → env≠CLI 参数，launch-script 模式脚本内必须自带
  `--middleware anomaly_middleware.AnomalyMiddleware`；deploy 已默认拦截（rc=3），
  `--force` 越过后得到的是无监控裸 vllm，verify V3/V4 会失败；
- **多实例默认都抢卡 0** → vllm-ascend/GPU 默认选首卡，第二实例必须 `--env
  ASCEND_RT_VISIBLE_DEVICES=N`（或 CUDA_VISIBLE_DEVICES）指定空闲卡，并用 npu-smi/nvidia-smi
  实测各卡归主；
- **把 npu-smi 物理卡号填进 ASCEND_RT_VISIBLE_DEVICES** → 真实事故：容器挂 davinci2/5，
  逻辑号是 0/1，填物理 5 → aclInit 107001 "Invalid device ID"，引擎启动即崩。
  该 env 只认【逻辑号】；先跑 probe 取 `npu_logical_count`/`npu_davinci`/空闲映射，deploy 对
  非法值已做 fail-fast（rc=3），校验不可用时强告警——此时必须先补证再拉起，不得拍脑袋填值；
- **verify rc=1 盲目续投，日志已崩溃还在等** → 真实事故：Traceback 滚出滚动窗口、末行是崩溃后
  打印的 DeprecationWarning，被当普通 progress 白等满窗口。现 verify 已在 rc=1 前做全量日志
  致命扫描；调用时**必须传 --log**（未传时 Agent 续投前必须自行 grep ERROR/Traceback，命中转
  rc=3 处理）；连续 2 轮 rc=1 先查进程/端口再续投；
- **部署完不登记 webui、不回写档案** → 部署完成的 exit criteria 是"收尾三件套"（V1-V4 过 +
  webui 登记 + 档案回写），缺一不算完成；webui 在跑但 `instances: []` 空着、或档案里查无 webui
  状态导致后续会话重查重搭，都是违反硬门槛的表现。webui 段/隧道/实际端口/结构化 verified 必须
  进档案——档案是下次会话的唯一起点；
- **停服后档案只处理一半** → 双轨约定：instances.yaml 条目保留 + 结构化 stopped 标记
  （删条目 = 丢启动参数，重启只能重问用户），webui.yaml 默认注销登记 + 拆隧道（留灰色
  离线卡片 = 看板噪音）；拉起请求先查档案再问用户——档案命中把"问参数"坍缩成一次复用确认。
- **对已存在 pkl 自作主张** → 目标 pkl 已存在 = 用户决策点（沿用追加/改名/删除/换路径四选一），
  probe 只报告不动作（save_path_file_exists/dir_pkls）；静默沿用会混淆历史数据归属，
  擅自删除 = 丢历史检出现场不可逆；
- **把 pkl 回传控制节点再生成报告当标准流程 / 为此预检控制节点三方依赖** → 标准 = 节点侧生成 +
  控制节点合并（W4.4/report-tool.md）：anomaly_report.py 在 pkl 所在节点的运行环境内执行（pkl 零传输），
  控制节点只跑 merge_reports.py（纯 stdlib，无需 numpy/PyYAML/transformers）；pkl 同步到控制节点
  属遗留备选路径（仅单机自查/离线深挖）；
- **报告执行位不跟随档案 runtime** → 真实教训：容器部署默认在宿主机跑报告，撞缺 transformers
  才转容器，多走两轮——执行位 = 验证位 = 档案 runtime（W4.3，不限容器：docker/venv/conda/bare），
  档案缺 runtime 记录先补档再验证，不猜；
- **middleware 能 import 就断言全套工具代码在** → 非 editable 安装（wheel/`pip install .`）
  site-packages 只有 anomaly_middleware 包，tools/、configs/ 不落盘；旧检出可能缺新报告工具或
  stats_version 不兼容——必须定点验证检出根内 tools/anomaly_report.py + configs/detector.yaml
  存在性与版本（W4.3 ③），缺/旧才从控制节点副本补投，双侧皆缺请用户检出仓库；
- **跳过检出根查缺直接投递工具包** → 拉起过监控 ⇒ 运行环境内必有 anomaly_middleware，editable
  安装 ⇒ 检出根多半有全套工具代码（真实教训：被测节点 /home/gyl/accuracy-monitoring 全套在
  （含 tools/），仍整包投递）——投递是最后手段，先查后投（W4.4 查缺顺序）；
- **报告/探活从节点自身访问公网 EIP** → 真实事故：容器（host 网络）内 curl 公网 EIP 被拒
  （Connection refused，EIP 不在本机网卡上），报告状态列误判"离线"——节点内探活/报告状态列
  用内网可达地址，档案 server 字段登记内网地址；

## 参考（references/，按需查阅）

| 文件 | 何时查 |
|---|---|
| [deployment-single.md](references/deployment-single.md) | 单机部署全集：env 表、SAVE_PATH 细则、tokenizer 解析顺序、fail-fast 清单 |
| [deployment-pd.md](references/deployment-pd.md) | PD 部署全集：词表预生成、三种集成模式、--anomaly-* 参数映射 |
| [remote-execution.md](references/remote-execution.md) | 远程执行协议：三层脚本契约、ansible 用法、W1~W5 执行归属表 |
| [metrics-reference.md](references/metrics-reference.md) | 全部指标定义、标签、PromQL 示例、三通道决策表 |
| [pkl-data-contract.md](references/pkl-data-contract.md) | pkl 记录字段契约、编号规则、读取示例（现场回查用） |
| [webui-guide.md](references/webui-guide.md) | webui 配置/告警规则/后端 API（密度问题读数用） |
| [report-tool.md](references/report-tool.md) | anomaly_report.py 完整用法、instances.yaml 档案 schema、报告结构 |
| [troubleshooting.md](references/troubleshooting.md) | 启动期 fail-fast 分类表 + 推理期无数据排查表 |
