# 远程执行协议（ansible 通道 / 三层脚本 / 执行归属）

## 通道纪律（仅 ansible，铁律 9）

- 对被测服务器的 shell 级操作（进程/卡/文件/服务）**一律经 ansible** 投递执行
  （`-m command` / `-m script`）；**禁止** ssh 直连操作、禁止默认本机操作；
  同机场景用 `ansible localhost --connection=local`——控制节点自身有没有卡/进程不影响
  本通道约定（不得假设"管理节点无 vllm 无加速卡"，以 probe 实测为准）；
- **ansible 未安装**：pipx 已存在 → Agent 可 `pipx install ansible`（或 `ansible-core`）尝试一次；
  pipx 不存在 → 用户安装（Agent 给命令），**不做其他变通尝试**（不尝试 apt/yum/docker/源码编译）；
- HTTP 类探测（metrics/config/模型端点/测试请求）可由任意可达节点直连，不走 ansible。

## 安装归属矩阵（谁装什么）

| 组件 | 归属 | 附加规则 |
|---|---|---|
| 系统级组件（ssh client、apt/yum 系统包） | **用户装**——Agent 只做：检测缺失 → 给精确安装命令 + 说明用途与限制 → 用户确认后复验 | ssh client 缺失时同时告知替代：paramiko 传输可免 ssh client（`ansible_ssh_transport=paramiko`） |
| ansible / pipx | pipx 已存在 → Agent 可 `pipx install ansible` 一次（用户级、无系统污染）；pipx 不存在 → 用户装 | 不尝试 apt 等系统级替代 |
| sshpass | **Agent 可自装**（低风险、非凭证、密码通道必需） | 网络失败即停（见下）；密钥模式完全不需要 |
| 目标环境内的业务组件（anomaly_middleware） | **用户在目标环境内装**——Agent 检测（probe/deploy 实测 import）→ 给安装命令 → 用户装好后复验；**Agent 不代装**（含容器 exec pip） | 安装命令：`cd /path/to/accuracy-monitoring/ && pip install -e .`（容器先 `docker exec -it <容器> bash` 进入）；wheel 离线/进镜像为替代方式 |
| 凭证（密码文件/密钥/vault） | **永远用户执行**，Agent 给命令模板 | 铁律 10 |

**环境准备清单**（输出模板——先说明、再动手；缺失检测先于安装）：

| 组件 | 用途 | 由谁装 | 安装命令 | 验证命令 | 限制 |
|---|---|---|---|---|---|
| ansible | 编排通道 | 用户 | pipx install ansible | ansible --version | 未入 PATH 时用全路径 |
| sshpass | 密码认证注入 | Agent 可代装 | apt/yum install sshpass | command -v sshpass | 仅密码模式需要；密钥模式免 |
| ssh client | ansible 默认传输 | 用户 | apt install openssh-client | command -v ssh | 通常已预装；缺失=极简环境；可切 paramiko 替代 |
| 密码文件 | 凭证引用 | 用户 | umask 077 && echo '密码' > ~/.ansible_cpass | ls -l ~/.ansible_cpass（应为 600） | 铁律 10；密码本体不进对话 |

流程：检测缺失 → 输出清单（含限制说明）→ 用户执行用户侧条目 → Agent 执行 Agent 侧条目 →
复验（`ansible --version` / ping 通目标）。

- **网络失败即停**（适用于一切网络依赖操作：pipx/pip/apt/yum 安装、sshpass 安装、ansible 连接、
  包/词表下载）：失败一次 → 贴错误原文 + 按下表判读 + 交用户处理；
  **禁止换源、配置代理、反复重试、安装隧道/VPN 工具**——网络策略是用户环境决策，Agent 永不代做：

  | 错误特征 | 判读 | 动作 |
  |---|---|---|
  | Could not resolve / name resolution | DNS 解析失败 | 停；用户检查 DNS/网络 |
  | Connection timed out / unreachable | 到目标/源网络不通 | 停；用户检查路由/防火墙/VPN |
  | 403/404（pip/npm/apt 源） | 源不可达或包不存在 | 停；换源/配代理属用户决策 |
  | Permission denied (publickey,password) | 认证失败（非网络问题） | 核对凭证引用（密码文件/密钥路径）后一次重试，仍失败停 |

## 三条执行归属原则

| 原则 | 内容 |
|---|---|
| shell 级操作一律经 ansible 落在服务所在节点 | 进程/卡/文件/服务类操作（ps、nvidia-smi、写 env、拉起服务、查 pkl）：经 ansible 显式指定目标计算节点投递，禁止 ssh 直连与默认本机执行（本机操作无 ansible 审计路径；控制节点是否有卡/vllm 以 probe 实测为准，不作豁免理由）；同机用 `ansible localhost --connection=local` |
| HTTP 类探测可任意可达节点直连 | metrics/config/模型端点/测试请求均为 HTTP；网络不可达时经 ansible 在目标机回环执行 |
| Agent 侧组件本机执行 | webui（轮询各实例的聚合方）、merge_reports.py 合并、密度问题读数——始终在 Agent 所在节点运行，不走 ansible；**报告生成例外**：anomaly_report.py 在 pkl 所在节点的运行环境内执行（执行位跟随档案 runtime；pkl 原地零传输），产出 fetch 回（容器内产出先 docker cp 出宿主机）；仅遗留备选路径（单机自查/离线深挖）才把 pkl 同步到 Agent 侧 `pkl_dir` 本机生成 |

## 三层脚本契约（`<SKILL_DIR>/scripts/`，自包含、免预装）

| 层 | 脚本 | 性质 | 职责 | 退出码 |
|---|---|---|---|---|
| probe | `probe_env.sh` | 只读、幂等、可重复 | 两种模式：**`--discovery` 轻量服务发现**（仅列 8xxx 端口运行中 vllm 服务：pid/端口/模型/middleware/监听态，秒级返回，跳过一切慢检查）；**全量部署检查**：python/包安装状态、vLLM 版本与 `--middleware` 支持、现有 vllm 进程与启动命令（是否已挂 middleware）、硬件类型与加速卡总数/空闲数（附属获取物）、监听端口、SAVE_PATH 可写性 | 0=探测完成（好坏看 facts）；2=用法错误 |
| exec | `deploy_monitor.sh` | 变更动作，**用户确认后才投递** | 预建落盘目录、写 env（含 `--env KEY=VALUE` 附加变量）、**端口顺位探测**（请求端口被占用自动 +1 至多 +20，成功后回显实际端口）、后台拉起 vllm serve --middleware（宿主机 setsid / `--docker <容器名>` 时 `docker exec -d`），**立即返回"已下发"**；launch-script 模式缺 `--middleware` 默认拒绝；`--force` 越过防重（多实例第二实例）；容器模式经 exec 安装 middleware（`--pkg-path`，**非持久**，容器重建需重装） | 0=已下发（JSON 含 requested_port/port）；1=执行失败/端口耗尽；3=参数缺失/launch-script 缺 --middleware；4=重复接入拒绝（--force 可越） |
| verify | `verify_anomaly.sh` | 只读、幂等；支持 `--wait` 活性轮询（`--log` 联动启动日志） | 就绪探活（metrics 200）→ 完整 V1-V4；等待期三判定：**进度**（日志末行随返回）、**停滞**（`--stall` 秒无日志增长）、**致命错误**（Traceback/端口占用/OOM/缺模块 → 立即失败） | **rc 语义**：0=就绪且 V1-V4 过；1=窗口内未就绪（Agent 自动续投下一轮，不问用户，累计 ≤40 分钟）；3=启动失败（附日志原文）；4=加载停滞（附进度末行）；2=用法错误 |

共同约定：stdout **末行为纯 JSON**（供程序化解析），此前为人类可读证据行；失败时 stderr 带中文原因。
`hardware`（NPU/GPU）为**附属获取物**：probe 采卡信息时按探测命令可用性顺带判定，不做专门获取，无则 `-`。

## ansible 用法约定

```shell
# 单行命令
ansible <目标主机模式> -i <inventory> -m command -a "..." -b

# 脚本投递（自动推送文件到远端执行，免预装、免副本漂移）
ansible <目标> -i <inventory> -m script -a "<SKILL_DIR>/scripts/probe_env.sh --tp 4" -b
```

- root：多数部署动作需 `-b`（become）；inventory/连接参数由用户提供，技能不发明；
- **docker 查询免 `{{}}` 片段**（ansible 会把 `{{ }}` 当 Jinja2 模板渲染，以下形态直接复制）：

  ```shell
  docker inspect <容器> | grep -o '"Running":[[:space:]]*[a-z]*'      # 运行状态
  docker inspect <容器> | grep -o '"Image": *"[^"]*"'                 # 镜像
  docker inspect <容器> | grep -i davinci                             # NPU 设备挂载
  docker ps | awk 'NR>1 {print $1, $NF}'                              # 容器 ID 与名称列
  docker port <容器>                                                  # 端口映射
  ```

- **复杂命令一律 `-m script`**：含 `$` 变量、多层引号嵌套、`/proc/*` 读取的远端命令
  （如 `/proc/<pid>/environ` 检查）——写入脚本经 `-m script` 投递执行，
  **禁止 `-m shell` 一行流**（引号嵌套与变量展开极易翻车，实测两次）；
- **回传契约**：判定依据 = ansible 回传的 **rc + stdout 原文**（证据纪律）；末行 JSON 供解析，
  二者缺一不可；
- **良性噪声标注**：已知无害输出（profile source 报错、locale 警告、DeprecationWarning 等）
  在引用证据时标注"良性噪声，可忽略"；rc 判定只看回传 rc 与目标输出，噪声不改变判定；
- **转义陷阱**：`-m script` 的 stdout 在 ansible 结果里是 JSON 转义字符串（如
  `{\"rc\":0,...}`），按字面 `"rc":0` grep 会漏配——程序化判定以**任务级 rc**（0=脚本正常退出）
  与 `[verify]` 等证据行原文为准，勿对转义后的 JSON 做子串匹配；
- **模板冲突**：远端命令含 `{{ }}`（如 `docker inspect --format` 的 Go 模板）会被 ansible
  当作 Jinja 模板解析而报 `Syntax error in template`——改用 grep/awk 在回传文本上提取，
  不要把 `{{ }}` 直接写进 `-a` 参数；
- 远端命令失败（127/权限拒绝/连接失败）≠ 能力不存在——先核对目标主机与回传 rc 再下结论；
- 多计算节点：目标主机模式显式指定；全量 fan-out 须用户显式确认；逐主机汇总结果，
  部分失败如实列出。

## vllm 拉起必须后台化

模型加载耗时 1~10 分钟：`deploy_monitor.sh` daemonize 后**立即返回"已下发"**，健康等待由
`verify_anomaly.sh` 轮询——ansible 不得同步等待整个启动过程（会挂死/伪超时）。

## 凭证纪律（强制，铁律 10 细则）

**四不入**——凭证本体（服务器密码/私钥/vault 口令）：

| 不入 | 原因 |
|---|---|
| 对话上下文 | 随会话历史落盘/导出，且会发送至模型服务商侧 |
| 服务档案（instances.yaml） | 档案只存"连接方式引用"：用户名、密钥路径、vault 文件名 |
| 报告与日志 | 报告会被归档/转发 |
| 命令行参数 | `sshpass -p xxx`、`--ask-pass` 管道回显会进 `ps` 与 shell history |

**合法形态**（按优先级）：

1. **SSH 密钥免密（首选）**：`ssh-keygen -t ed25519` + `ssh-copy-id <user>@<计算节点>`；`-b` 提权
   依赖被控节点 NOPASSWD sudo（自动化场景必须免密或引用化，Agent 无法交互输密码）；
2. **密码受控通道**（密钥分发受限环境，允许使用密码）：
   - 用户在管理节点**自己执行**（密码不进对话，Agent 只给命令模板）：
     ```bash
     umask 077 && echo '你的密码' > ~/.ansible_cpass
     ```
   - 用户仅告知 Agent **文件路径**（引用，可登记服务档案）；
   - Agent 调用：
     ```bash
     ansible <host> -i <inventory> -u <用户名> --connection-password-file ~/.ansible_cpass -b ...
     ```
      （ansible ≥ 2.13；旧版改用环境变量 `ANSIBLE_CONNECTION_PASSWORD_FILE` 指向该文件）；
    - **首次连接 host key**：非交互场景（ansible ad-hoc/script）遇未知主机指纹会卡在确认——
      命令前加环境变量 `ANSIBLE_HOST_KEY_CHECKING=False`（或 inventory 侧配
      `host_key_checking = False`）；端口非 22 时 inventory 用 `host:port,` 内联写法
      （如 `-i '1.2.3.4:2123,'`，注意末尾逗号）；
   - **前置**：密码认证走 sshpass——管理节点未装则 Agent 安装（工具安装是合法动作，不是凭证）；
   - 密码本体绝不入对话/档案/命令行；用后用户可自行删除文件或改密；
3. **ansible-vault**：加密变量文件（`chmod 600`）+ vault 口令走环境变量
   （如 `ANSIBLE_VAULT_PASSWORD_FILE` 指向 `chmod 600` 的口令文件）；
   使用最小权限专用账号（仅部署目录/服务管理 sudo），用后改密。

**Agent 行为约定**：

- 用户主动粘贴密码进对话时 → 不复述、不落盘、不写入任何命令行，引导转为**密码受控通道**
  （给命令模板让用户自己创建密码文件，Agent 仅引用路径）；
- 用户告知密码**文件路径** → 属于"引用"，允许登记服务档案并直接使用；
- **凭证文件复验按模式搜索**：`ls ~/.ansible* 2>/dev/null`——命中近似名
  （如 `.absible_cpass`）→ 提示疑似笔误并请用户确认；未命中 → 如实报告未找到，
  不臆测路径；
- 连接失败涉及认证时 → 只报告"认证失败"，不回显任何已知的凭证材料。

## W1~W5 执行归属表

| 步骤 | 执行位置 | 方式 |
|---|---|---|
| 服务发现与状态探测 | 服务所在节点 | probe_env.sh（ansible `-m script` 投递）+ 在线服务列举（进程/端口/模型） |
| 前置检查/资源校验数据采集 | 服务所在节点 | probe facts |
| 分支判定 / 征得同意 / 落盘询问 / 变更清单 / 证据判读 | Agent 侧 | Agent 与用户交互（不进脚本） |
| 拉起/重启 | 服务所在节点 | deploy_monitor.sh -b（后台化；拉起前二次确认"是否现在拉起"） |
| C3 验证 V1-V4 | 服务所在节点回环（首选）或任意可达节点直连 | verify_anomaly.sh（`--wait 300 --log <日志>` 活性轮询）/ curl |
| V5 pkl 核验 | 服务所在节点 | ansible `-m command`（ls/stat） |
| webui 拉起 + 实例注册 | Agent 侧本机 | 不走 ansible |
| 报告生成（anomaly_report.py） | pkl 所在节点的运行环境内（执行位跟随档案 runtime） | 先执行环境定点验证（W4.3：pkl/依赖/工具代码）→ 查缺顺序：检出根 repo_checkout 已有全套只投最小 instances.yaml → 旧版/缺文件从控制节点副本补投 → 双侧皆缺请用户检出仓库（pkl 零传输）；产出 html/md/stats.json fetch 回 |
| 报告合并（merge_reports.py）/ 密度读数 | Agent 侧本机 | 不走 ansible；webui API / PromQL |
| pkl 同步到 pkl_dir（遗留备选，仅单机自查/离线深挖） | 服务所在节点 → Agent 侧 | ansible fetch / rsync |
