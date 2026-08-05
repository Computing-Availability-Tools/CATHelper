# CI Pipeline Gate 配置指南

本特性为 CATHelper 仓库配置 GitHub Actions 流水线门禁。门禁作为 required status check，在 PR 合入 `main` / `feature/*` 分支前必须全部通过。

## 1. 门禁内容

workflow 文件：`.github/workflows/ci.yml`

| Job 名（即 status check 名） | 模块 | 执行内容 |
|------|------|----------|
| `catmonitor` | `CATMonitor/` (Go) | `go build ./...` + `go vet ./...` + `go test ./...` |
| `straggler` | `feature/straggler/` (Go, 独立 module) | `go build ./...` + `go vet ./...` + `go test ./...` |
| `elastic-ep-demo` | `feature/elastic-ep/examples/fault_tolerance_scale/` (Python) | `python -m unittest discover -p 'test_*.py'` |

触发条件：push 或 PR 到 `main` / `feature/**` / `fix/**`。

> 依赖 vLLM+NPU 的 `feature/elastic-ep/tests/v1/fault_tolerance/` 测试**不纳入** CI，需在 NPU 环境单独运行。

## 2. 部署步骤

### 2.1 合入门禁 workflow（必需，顺序不可颠倒）

Required status check 必须**先存在**才能配置 branch protection，因此必须先合入 CI workflow：

1. 在 fork 仓库（`a798347923/CATHelper`）创建分支
2. 提交 `.github/workflows/ci.yml`
3. 推送并创建 PR 到 upstream（`Computing-Availability-Tools/CATHelper`）
4. 合入 PR，确认三个 job 在 upstream main 上运行成功

### 2.2 前置检查

在具备 upstream `Computing-Availability-Tools/CATHelper` 仓库 **Admin 权限** 的环境中执行：

```bash
gh auth status
# 确认已登录且对 upstream 有 admin 权限

# 确认 upstream 上三个 status check 已存在（合入 2.1 后应有结果）
gh api repos/Computing-Availability-Tools/CATHelper/actions/runs --paginate \
  --jq '.workflow_runs[0:5] | map({name: .name, status: .status, conclusion: .conclusion})'
```

### 2.3 配置 main 分支保护

将以下 JSON 保存为 `/tmp/protection-main.json`：

```json
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["catmonitor", "straggler", "elastic-ep-demo"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
```

执行：

```bash
gh api -X PUT repos/Computing-Availability-Tools/CATHelper/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input /tmp/protection-main.json
```

### 2.4 配置 feature/* 分支保护

将以下 JSON 保存为 `/tmp/protection-feature.json`：

```json
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["catmonitor", "straggler", "elastic-ep-demo"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
```

执行：

```bash
gh api -X PUT repos/Computing-Availability-Tools/CATHelper/branches/feature%2A/protection \
  -H "Accept: application/vnd.github+json" \
  --input /tmp/protection-feature.json
```

> 注意：通配分支名 `feature/*` 在 URL 中需编码为 `feature%2A`（如上例）。

### 2.5 验证

```bash
gh api repos/Computing-Availability-Tools/CATHelper/branches/main/protection \
  --jq '{required: .required_status_checks.contexts, strict: .required_status_checks.strict, reviews: .required_pull_request_reviews.required_approving_review_count}'
```

预期输出：

```json
{
  "required": ["catmonitor", "straggler", "elastic-ep-demo"],
  "strict": true,
  "reviews": 1
}
```

## 3. 撤销配置（如需）

```bash
gh api -X DELETE repos/Computing-Availability-Tools/CATHelper/branches/main/protection
gh api -X DELETE repos/Computing-Availability-Tools/CATHelper/branches/feature%2A/protection
```
