# straggler-detection

Agent skill：从 Ascend PyTorch Profiler `.db` 检测 AI 训练/推理集群中的**慢节点（straggler / 亚健康）**——慢计算卡、慢通信域 / PP 通信组、慢 CPU 卡、NPU 空泡，并输出逐类别汇总表与自然语言结论。

## 安装（npm / npx）

```bash
npx @nifeng15423/straggler-detection             # 默认 → ~/.agents/skills（OpenCode / Claude 系可发现）
npx @nifeng15423/straggler-detection --opencode  # → ~/.config/opencode/skills
npx @nifeng15423/straggler-detection --project    # → 当前项目 ./.agents/skills
npx @nifeng15423/straggler-detection --dir <路径> # 自定义 skills 根目录
```

安装器只拷贝本 skill 自己的文件（`SKILL.md`、`README.md`、`LICENSE`、`scripts/`、`references/`），不影响目标目录里的其它 skill。

## 使用

```bash
python scripts/main.py path=/path/to/data compute=1.3 io=2.5 comm=1.3 pp_comm=5 clean=ask
```

- 运行环境：Python 3.8+，**仅标准库**，无第三方依赖、无需编译。
- 完整说明（触发条件、四组阈值、执行前询问流程、结论输出规范）见 **`SKILL.md`**。
- 算法细节见 **`references/detection.md`**。

## 许可

Mulan PSL v2（木兰宽松许可证第 2 版），见 `LICENSE`。
