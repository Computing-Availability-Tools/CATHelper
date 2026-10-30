# pkl 数据契约（异常现场回查用）

## 产生条件与路径

- 由环境变量 `VLLM_ANOMALY_SAVE_PATH` 控制：**未设置 → 不落盘**（仅内存编号，重启归零）；
- 值以 `.pkl` 结尾 → 文件模式（路径即文件）；否则 → 文件夹模式（文件名=`<模型名>.pkl`）；
- 启动期校验：绝对路径；文件夹模式目录/文件模式父目录须预先存在（投递脚本会预创建）。

## 文件结构

pickle 序列化的 dict：`{异常编号(int): 记录(dict)}`。

## 记录字段

| 字段 | 说明 |
|---|---|
| `time` | 检出时间（Unix 秒）——按时间窗过滤报告数据的依据 |
| `prompt` | 请求 prompt：chat 为 `messages` 列表（含 role：system/user/assistant/tool）；completions 为 str 或 list |
| `ill_type` | 异常类型（数值）：1=生僻字 2=乱码 3=重复 4=NaN |
| `topk_logprobs` | topk logprobs 数据（列表，与 tokens_ids 同构） |
| `tokens_ids` | topk token_id 数据（列表） |
| `text_tokenids` | 推理输出 token ids（新版记录含此字段；旧版缺失时展示序列回退 `tokens_ids[:,0]`） |
| `text` | 该候选推理输出文本 |
| `model_name` | 模型名称 |

## 编号规则

- 落盘开启时，首次保存读文件取 `max(内存计数器, 文件 key 最大值)` 同步后累加——**重启续接不覆盖**；
- 编号自增不回退；保存失败（IO/权限）→ 记日志，不回退编号，不影响客户端与后续检测；
- 按异常候选粒度保存：单请求多候选多异常 → 多条记录、多个编号，互不覆盖。

## 读取示例（回查某编号）

```python
import pickle, time

with open("/data/anomaly/qwen3.pkl", "rb") as f:
    data = pickle.load(f)

rec = data[17]                       # 异常编号 17
print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["time"])))
print("ill_type:", rec["ill_type"])  # 1=生僻字 2=乱码 3=重复 4=NaN
print("model:", rec["model_name"])
print("output:", (rec["text"] or "")[:200])   # 截断展示
# prompt 为多轮 messages 列表（chat）或字符串（completions），完整查看注意脱敏
```

## 回查注意事项

1. **展示截断脱敏**：prompt/输出文本先截断（如前 200 字符）再展示，完整内容按编号回查；
2. **计数口径**：分布统计以记录的 `ill_type`（线上检出事实）为准；
3. **旧版兼容**：无 `text_tokenid` 字段的旧记录，展示序列自动回退 `tokens_ids[:,0]`；
4. **多轮归因**：异常可能由第 N 轮的工具输出而非最后一条 user 消息触发，全文本与末轮分开看；
5. **Agent 重试风暴**：同一 prompt 可能因框架重试产生近重复记录，按需按内容哈希去重；
6. **pkl 在计算节点产生**：标准流程报告在 pkl 所在节点生成（pkl 原地零传输；全局服务档案中
   登记 `pkl_file` 文件名）；仅遗留备选路径（控制节点本机生成/离线深挖）才需先同步到 `pkl_dir`
   （ansible fetch / rsync）。
