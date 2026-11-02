# straggler-detection

Agent skill：从 Ascend PyTorch Profiler `.db` 检测 AI 训练/推理集群中的**慢节点（straggler / 亚健康）**——慢计算卡、慢通信域 / PP 通信组、慢 CPU 卡、NPU 空泡，输出逐类别汇总表与自然语言结论。

## 安装（npm / npx）

```bash
npx @nifeng15423/straggler-detection             # 默认 → ~/.agents/skills（OpenCode / Claude 系可发现）
npx @nifeng15423/straggler-detection --opencode  # → ~/.config/opencode/skills
npx @nifeng15423/straggler-detection --claude    # → ~/.claude/skills
npx @nifeng15423/straggler-detection --project   # → 当前项目 ./.agents/skills
npx @nifeng15423/straggler-detection --dir <路径> # 自定义 skills 根目录
```

安装器只拷贝本 skill 自己的文件（`SKILL.md`、`README.md`、`LICENSE`、`scripts/`、`references/`），不影响目标目录里的其它 skill。

## 使用

安装后**不需要自己敲命令**——在 Agent（OpenCode / Claude 等）里直接用自然语言描述问题即可，例如：

> 用 straggler-detection 分析 `/path/to/profiler_output` 这批 profiling 数据：训练时部分卡明显偏慢、step 耗时变高，看看是哪张卡 / 哪个通信域的问题。

Agent 会识别到本 skill，先按流程向你确认参数（计算 / IO / 通信 / PP 四组阈值、是否清理中间数据、是否 `no-align`），再解析 `.db`、执行检测，最后给出：

- **逐类别汇总表**（异常卡 / 通信组 / 劣化指数 / 阈值）
- **自然语言结论**（哪张卡 / 哪个组、相对组内基线几倍）
- 检测结果与报告的**绝对路径**

输入：Ascend PyTorch Profiler 的 `.db` 目录（每卡一个），或单文件 cluster db。运行环境 Python 3.8+、仅标准库。

<details>
<summary>手动运行（开发 / 调试用，普通用户不需要）</summary>

```bash
python scripts/main.py path=/path/to/data compute=1.3 io=2.5 comm=1.3 pp_comm=5 clean=ask
```

</details>

详细执行规则见 `SKILL.md`，算法细节见 `references/detection.md`。

## 许可

Mulan PSL v2（木兰宽松许可证第 2 版），见 `LICENSE`。
