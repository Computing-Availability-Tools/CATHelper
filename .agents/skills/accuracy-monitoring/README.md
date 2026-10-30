# accuracy-monitoring skill

vLLM 推理精度异常监控技能——指导 Agent 完成 vLLM 推理精度异常检测（生僻字/乱码/重复输出/NaN）
在单机与 PD 分离两种形态下的接入与验证，通过 Prometheus 指标 / Web 看板 / pkl 落盘三个通道
检视检出结果，并对任意时间窗生成《推理异常数据分析报告》。

## 简介

- **检测对象**：四类异常——1=生僻字、2=乱码、3=重复输出、4=NaN（logprobs 出现 nan/inf）；
- **两种接入形态**：单机（`--middleware` 插件随 `vllm serve` 进程）与 PD 分离（检测上移至
  load_balance_proxy，P/D 节点启动命令保持纯净）；对客户端透明，不影响推理响应；
- **三个结果通道**：`GET /anomaly/metrics`（Prometheus 文本，机器读）、Web 看板（多实例聚合 +
  阈值告警）、pkl 落盘（`VLLM_ANOMALY_SAVE_PATH`，异常全量现场）；
- **能力清单**：部署接入（单机/PD）、部署验证（V1-V4 证据闭环）、运行态检视与采样率热调
  （`/anomaly/config`）、任意时间窗异常报告、停服与档案复用。

## 安装

### 1、手动安装

将该文件夹直接复制到对应 Agent 的技能目录：

| Agent | 项目级 | 全局 |
|---|---|---|
| opencode | `<项目>/.opencode/skills/` | `~/.config/opencode/skills/` |
| claude | `<项目>/.claude/skills/` | `~/.claude/skills/` |
| 通用 | `<项目>/.agents/skills/` | `~/.agents/skills/` |

也可通过软链接把技能目录指向具体 Agent 的技能目录（随仓库更新即时生效）。
```
# 软连接示例
ln -s "$(pwd)/.agent/skills/accuracy-monitoring" ~/.config/opencode/skills/
```

### 2、npx 自动安装

```bash
npx accuracy-monitoring              # 默认安装到 opencode 全局技能目录（~/.config/opencode/skills/）
npx accuracy-monitoring --project    # 安装到当前项目 .opencode/skills/
npx accuracy-monitoring --dir <path> # 指定路径安装
npx accuracy-monitoring --link       # 开发模式：软链到本仓库检出
npx accuracy-monitoring --force      # 目标已存在时覆盖
npx accuracy-monitoring uninstall [--project | --dir <path>]
```

报告生成与 PD 词表工具依赖 accuracy-monitoring 仓库（`tools/` + `anomaly_middleware/`），
建议安装时登记仓库根（写入 `.repo`，供技能解析）：

```bash
npx accuracy-monitoring --repo /path/to/accuracy-monitoring
```

## 快速使用

安装后重开 Agent 会话使技能被识别，直接说一句即可触发，例如：

- "帮我给这个 vllm 服务加上精度监控"
- "确认一下异常检测在跑吗"
- "看看现在有没有异常" / "采样率调到 0.3"
- "出一份最近 7 天的异常报告"
- "某类异常最近有没有变密"

## 关键约束

- **证据闭环**：生效唯一判据 = 验证清单 V1-V4 通过（metrics 200 + 计数/采样率可读 + 请求头
  命中 + 计数增长）；"启动没报错/进程存活"不是生效证据；
- **先档案、后提问、再探测**：优先查全局服务档案（`configs/instances.yaml`），缺失字段才问
  用户，禁止漫游目标机目录/翻找模型文件；
- **远程执行一律经 ansible**：禁止 ssh 直连与默认本机操作（同机走
  `ansible localhost --connection=local`）；凭证引用化，密码本体不入对话/档案/命令行；
- **仓库依赖**：报告生成与 PD 词表工具依赖仓库（解析顺序：`ACCURACY_MONITOR_REPO` →
  仓库同构布局 → `<SKILL_DIR>/.repo` 登记 → 询问用户）；指标/看板/pkl 落盘不依赖仓库；
- **落盘如实说明**：未设 `VLLM_ANOMALY_SAVE_PATH` 时无 pkl，明细报告无法生成——如实告知并
  给开启步骤，不伪造明细。

完整工作流（W1~W5）、铁律清单与分场景参考资料见 [SKILL.md](SKILL.md) 与
[references/](references/)。
