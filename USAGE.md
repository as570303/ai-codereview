# ai-codereview 使用与配置指南

> 基于 Anthropic Claude 的多维度 AI 代码审查工具，支持单文件、整目录、Git diff 三种模式。

---

## 目录

- [快速开始](#快速开始)
- [安装](#安装)
- [CLI 命令](#cli-命令)
  - [审查单文件](#审查单文件-file)
  - [审查整个目录](#审查整个目录-directory)
  - [审查 Git diff](#审查-git-diff-diff)
  - [通用选项](#通用选项)
  - [退出码](#退出码)
- [配置文件](#配置文件-codereviewyml)
  - [完整配置示例](#完整配置示例)
  - [配置项说明](#配置项说明)
- [环境变量](#环境变量)
- [忽略路径](#忽略路径-ignore_paths)
- [输出格式](#输出格式)
- [增量基线](#增量基线)
- [自定义规则](#自定义规则)
- [本地模型（Ollama）](#本地模型ollama)
- [评估黄金测试集](#评估黄金测试集)
- [常见问题](#常见问题)

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
echo "ANTHROPIC_API_KEY=sk-ant-xxxx" > .env

# 3. 审查单个文件
python review.py file src/app.py

# 4. 审查整个目录
python review.py directory ./src

# 5. 只审查本次 Git 变更
python review.py diff HEAD~1
```

---

## 安装

### 依赖要求

- Python ≥ 3.11
- Anthropic API Key（使用 Claude 云端模型时必须）

### 安装方式

```bash
# 方式一：直接安装依赖
pip install -r requirements.txt

# 方式二：作为包安装（支持 codereview 命令）
pip install -e .

# 需要 Git diff 功能时额外安装 gitpython
pip install -e ".[git]"
```

安装为包后，可直接使用 `codereview` 命令替代 `python review.py`：

```bash
codereview file src/app.py
codereview directory ./src
codereview diff HEAD~1
```

---

## CLI 命令

### 审查单文件 `file`

```bash
python review.py file <文件路径> [选项]
```

```bash
# 示例
python review.py file src/auth.py
python review.py file src/auth.py --output ./reports/auth-review.md
python review.py file src/auth.py --no-desensitize  # 关闭脱敏（仅测试用）
```

### 审查整个目录 `directory`

```bash
python review.py directory [目录路径] [选项]
```

```bash
# 示例
python review.py directory ./src
python review.py directory .                    # 审查当前目录
python review.py directory ./src --max-files 20  # 最多审查 20 个文件
python review.py directory ./src --incremental   # 只报告新增问题
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--max-files` | `50` | 最多审查文件数，避免大仓库一次消耗过多 token |

### 审查 Git diff `diff`

审查相对于某个 commit/branch 的所有变更文件，只关注改动部分，节省 token。

```bash
python review.py diff [基准 commit] [选项]
```

```bash
# 示例
python review.py diff               # 默认对比 HEAD~1
python review.py diff main          # 对比 main 分支
python review.py diff abc123        # 对比指定 commit
python review.py diff HEAD~3 --context 30  # 保留 30 行上下文
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--context` | `20` | diff 前后保留的上下文行数 |

> **依赖**：`diff` 命令需要 gitpython，请先 `pip install gitpython`。

### 通用选项

所有三个命令均支持以下选项：

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `-c, --config` | `.codereview.yml` | 配置文件路径 |
| `-o, --output` | 配置文件中的值 | 报告输出路径（覆盖配置） |
| `--local` | `false` | 使用本地 Ollama 模型，不调用 Claude API |
| `--no-desensitize` | `false` | 关闭敏感信息脱敏（仅测试环境使用） |
| `--incremental` | `false` | 只报告相对于基线的新增问题 |
| `--update-baseline` | `false` | 审查完成后将本次问题写入基线文件 |

### 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 审查完成，无 Critical 问题 |
| `1` | 发生错误（文件不存在、API 调用失败等） |
| `2` | 审查完成，**发现 Critical 问题**（适合 CI 阻断合并） |

---

## 配置文件 `.codereview.yml`

工具默认从当前目录读取 `.codereview.yml`，也可通过 `--config` 指定路径。
**文件不存在时自动使用所有默认值，无需强制创建。**

### 完整配置示例

```yaml
# ── 模型配置 ──────────────────────────────────────────────────────────────────
model: claude-sonnet-4-6          # 使用的 Claude 模型
temperature: 0.0                  # 模型温度，范围 [0.0, 1.0]，0 最稳定
max_output_tokens: 8192           # 单次 API 调用最大输出 token 数

# ── 审查行为 ──────────────────────────────────────────────────────────────────
language: auto                    # 代码语言，auto=自动检测；也可指定 python/go/java 等
severity_threshold: low           # 报告阈值：只展示 >= 此级别的问题
                                  # 可选值：critical / high / medium / low
desensitize: true                 # 是否自动脱敏（API Key、密码、Token 等）
max_file_size_kb: 512             # 单文件最大读取大小（KB），超出则跳过

# ── 忽略路径 ──────────────────────────────────────────────────────────────────
ignore_paths:
  - "*.pyc"
  - "__pycache__"
  - "node_modules"
  - "vendor"
  - ".venv"
  - "dist"
  - "build"
  - "tests/fixtures/*"
  - "migrations/*"

# ── 自定义规则 ────────────────────────────────────────────────────────────────
custom_rules_path: ./rules/company-rules.md   # 公司/团队编码规范文件路径

# ── 增量基线 ──────────────────────────────────────────────────────────────────
baseline_path: ./.codereview-baseline.json    # 基线文件路径

# ── 输出配置 ──────────────────────────────────────────────────────────────────
output:
  formats:
    - markdown                    # 输出 Markdown 报告（可读性好）
    - sarif                       # 同时输出 SARIF（供 GitHub Code Scanning 消费）
  report_path: ./code-review-report.md

# ── 评分权重 ──────────────────────────────────────────────────────────────────
scoring:
  critical_weight: 20             # 每个 Critical 问题扣 20 分
  high_weight: 10                 # 每个 High 问题扣 10 分
  medium_weight: 3                # 每个 Medium 问题扣 3 分
  low_weight: 1                   # 每个 Low 问题扣 1 分

# ── 并发配置 ──────────────────────────────────────────────────────────────────
concurrency:
  max_workers: 5                  # 最大并发文件数（directory/diff 命令）
  rate_limit_rpm: 50              # API 请求限速（次/分钟），防止触发 429

# ── 重试配置 ──────────────────────────────────────────────────────────────────
retry:
  max_attempts: 3                 # 最大重试次数
  backoff_base: 2.0               # 指数退避底数（首次重试 ~2s，二次 ~4s）
  backoff_max: 30.0               # 单次等待最长秒数
  timeout: 60.0                   # 单次 API 调用超时秒数

# ── 本地模型（可选，需先启动 Ollama）─────────────────────────────────────────
use_local: false
local_base_url: http://localhost:11434/v1
local_model: codellama
```

### 配置项说明

#### 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | string | `claude-sonnet-4-6` | Anthropic 模型 ID。可选：`claude-opus-4-6`（最强）、`claude-haiku-4-5`（最快最便宜） |
| `temperature` | float | `0.0` | 模型温度 `[0.0, 1.0]`。审查场景推荐 `0.0` 以获得最稳定的输出 |
| `language` | string | `auto` | `auto` 根据文件后缀自动检测；也可显式指定如 `python`、`typescript` |
| `severity_threshold` | string | `low` | 过滤阈值，只报告 >= 该级别的问题。`critical` 最严格，`low` 报告全部 |
| `desensitize` | bool | `true` | 发送给 LLM 前自动遮蔽 API Key、密码、Token、Bearer Token 等敏感信息 |
| `max_file_size_kb` | int | `512` | 单文件读取上限（KB）。超过此大小的文件跳过审查并记录警告 |
| `max_output_tokens` | int | `8192` | 单次 LLM 响应最大 token 数。问题非常多时可适当增大 |
| `ignore_paths` | list | `[]` | 忽略的路径模式列表（见[忽略路径](#忽略路径-ignore_paths)） |
| `custom_rules_path` | string | `null` | 自定义规则文件路径（见[自定义规则](#自定义规则)） |
| `baseline_path` | string | `./.codereview-baseline.json` | 增量基线文件路径 |
| `use_local` | bool | `false` | 是否使用本地 Ollama 模型，`true` 时不消耗 Anthropic API 配额 |
| `local_base_url` | string | `http://localhost:11434/v1` | Ollama API 地址 |
| `local_model` | string | `codellama` | Ollama 模型名称 |

#### `output` 子配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `formats` | list | `["markdown"]` | 输出格式列表。有效值：`markdown`、`sarif`，不区分大小写 |
| `report_path` | string | `./code-review-report.md` | 报告保存路径（Markdown 格式的基准路径，SARIF 自动改为 `.sarif` 后缀） |

#### `scoring` 子配置（评分权重）

满分 100 分，每发现一个问题按权重扣分，最低为 0。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `critical_weight` | int | `20` | 每个 Critical 问题的扣分值 |
| `high_weight` | int | `10` | 每个 High 问题的扣分值 |
| `medium_weight` | int | `3` | 每个 Medium 问题的扣分值 |
| `low_weight` | int | `1` | 每个 Low 问题的扣分值 |

#### `concurrency` 子配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_workers` | int | `5` | 并发审查的最大文件数（目录/diff 模式）。必须 > 0 |
| `rate_limit_rpm` | int | `50` | API 请求限速（次/分钟）。低于 Anthropic 免费层限额时可降低此值 |

#### `retry` 子配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_attempts` | int | `3` | 包含首次请求的最大尝试次数 |
| `backoff_base` | float | `2.0` | 指数退避底数。首次重试等待约 2s，第二次约 4s |
| `backoff_max` | float | `30.0` | 单次等待上限（秒） |
| `timeout` | float | `60.0` | 单次 API 调用超时（秒）。大文件 chunk 较多时可适当增大 |

---

## 环境变量

优先级：环境变量 > `.env` 文件。

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `ANTHROPIC_API_KEY` | 是（云端模式） | Anthropic API Key，以 `sk-ant-` 开头 |
| `CODEREVIEW_PROMPT_VERSION` | 否 | 覆盖 Prompt 版本号，用于 CI 注入版本标识，默认 `v1.0` |
| `CODEREVIEW_PRICING_<MODEL>` | 否 | 覆盖模型定价，格式 `input:output`（USD/1M tokens）<br>例：`CODEREVIEW_PRICING_CLAUDE_SONNET_4_6=3.0:15.0` |
| `CODEREVIEW_SARIF_URI` | 否 | SARIF 报告中 `informationUri` 字段值（工具主页链接） |

**推荐通过 `.env` 文件管理密钥：**

```bash
# .env（加入 .gitignore，切勿提交）
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxx
```

---

## 忽略路径 `ignore_paths`

支持 glob 模式，可匹配文件名、目录名或完整路径片段。

```yaml
ignore_paths:
  # 文件扩展名
  - "*.pyc"
  - "*.min.js"
  - "*.pb.go"          # protobuf 生成文件

  # 整个目录（自动跳过目录内所有文件，不再递归进入）
  - "node_modules"
  - "vendor"
  - ".venv"
  - "__pycache__"

  # 路径通配
  - "dist/*"
  - "build/*"
  - "migrations/*"
  - "tests/fixtures/*"

  # 特定文件
  - "setup.py"
  - "manage.py"
```

> 目录名匹配（如 `node_modules`）会在 `os.walk` 阶段直接剪枝，不会枚举目录内的文件，性能高效。

---

## 输出格式

### Markdown 报告

默认格式，包含：问题总览表、评分、每个问题的文件/行号/描述/建议、审查文件列表。

```yaml
output:
  formats: [markdown]
  report_path: ./code-review-report.md
```

### SARIF 报告

[SARIF 2.1.0](https://sarifweb.azurewebsites.net/) 格式，可直接上传到 GitHub Code Scanning、SonarQube 等工具。

```yaml
output:
  formats: [markdown, sarif]   # 同时输出两种格式
  report_path: ./code-review-report.md
  # SARIF 文件自动保存为 ./code-review-report.sarif
```

**GitHub Actions 集成示例：**

```yaml
# .github/workflows/codereview.yml
- name: Run AI Code Review
  run: python review.py diff ${{ github.base_ref }}
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}

- name: Upload SARIF
  uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: code-review-report.sarif
```

---

## 增量基线

基线机制用于过滤"已知问题"，CI 中只报告**本次新引入**的问题，避免大量历史噪音。

### 工作流程

```bash
# 第一次：建立基线（将现有所有问题标记为已知）
python review.py directory ./src --update-baseline

# 后续 CI：只报告新增问题
python review.py diff main --incremental

# 定期更新基线（修复问题后）
python review.py directory ./src --update-baseline
```

### 基线文件格式

```json
{
  "version": 2,
  "hashes": [
    "a3f8c1d2e4b6...",
    "9e7b2a1c3f5d..."
  ]
}
```

**哈希算法**：SHA-256，基于 `文件路径 | 维度 | 标题`（不含行号和 severity，代码重构或 LLM 重新评级不会使基线失效）。

> v1 基线（MD5）与 v2 不兼容，加载时自动检测并提示重建。

---

## 自定义规则

通过 Markdown 文件为 LLM 补充团队特定的编码规范，这些规则会拼接到 System Prompt 末尾。

```yaml
custom_rules_path: ./rules/company-rules.md
```

**规则文件示例 `company-rules.md`：**

```markdown
## 公司编码规范

### 安全要求
- 所有数据库查询必须使用参数化查询，禁止字符串拼接 SQL
- 用户输入必须通过 `validate_input()` 验证后才能使用
- 密钥必须从环境变量或 Vault 读取，禁止硬编码

### 命名规范
- 常量命名必须全大写加下划线，如 `MAX_RETRY_COUNT`
- 私有方法必须以单下划线开头

### 接口规范
- 所有 HTTP 接口必须有超时设置（不超过 30s）
- 分页接口的 page_size 必须有上限校验（最大 100）
```

---

## 本地模型（Ollama）

不消耗 Anthropic API 配额，适合内网/离线环境，但审查质量低于 Claude 云端模型。

### 配置步骤

```bash
# 1. 安装并启动 Ollama
ollama serve

# 2. 拉取代码审查模型
ollama pull codellama
# 或更强的模型
ollama pull deepseek-coder:33b

# 3. 配置文件中开启本地模式
```

```yaml
use_local: true
local_base_url: http://localhost:11434/v1
local_model: codellama    # 或 deepseek-coder:33b
```

```bash
# 或命令行临时开启
python review.py file src/app.py --local
```

---

## 评估黄金测试集

`eval.py` 用于衡量 Prompt/模型版本的 Precision / Recall / F1，帮助量化审查质量。

```bash
python eval.py run --dataset ./golden_dataset
python eval.py run --dataset ./golden_dataset --prompt-version v1.1
python eval.py run --dataset ./golden_dataset --output ./eval-result.json
```

### 自定义期望结果

```bash
python eval.py run \
  --dataset ./my_test_cases \
  --expectations ./my_expectations.json
```

**期望文件格式 `expectations.json`：**

```json
{
  "security/sql_injection.py": ["SEC"],
  "logic/race_condition.go":   ["LOGIC"],
  "performance/n_plus_one.py": ["PERF"],
  "clean/no_issues.py":        []
}
```

前缀含义：`SEC`=安全、`LOGIC`=逻辑、`PERF`=性能、`QUAL`=质量。

**评估指标目标：**

| 指标 | 目标 | 含义 |
|------|------|------|
| Precision | ≥ 75% | 报告的问题中，真正有效的比例（低误报） |
| Recall | ≥ 80% | 已知问题中，被 AI 发现的比例（低漏报） |
| F1 | ≥ 77% | 综合指标 |

退出码 `1` 表示评估未达标，适合 CI 检测 Prompt 质量回退。

---

## 常见问题

**Q: `ANTHROPIC_API_KEY 未设置` 报错**

确保 `.env` 文件存在且 key 不含空白字符：
```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

**Q: 文件太大被跳过**

增大 `max_file_size_kb`：
```yaml
max_file_size_kb: 1024   # 允许最大 1MB
```

**Q: 触发 API 429 限速**

降低并发和速率：
```yaml
concurrency:
  max_workers: 2
  rate_limit_rpm: 20
```

**Q: diff 命令提示"不是 Git 仓库"**

在 git 仓库根目录执行，或确保 gitpython 已安装：
```bash
pip install gitpython
```

**Q: 审查报告问题行号不准确**

行号前标有 ⚠️ *行号待人工确认* 时，表示 LLM 报告的行号超出文件实际行数，属于正常现象（尤其在 chunk 审查模式下），人工对照代码确认即可。

**Q: 如何只看 Critical 和 High 问题**

```yaml
severity_threshold: high
```

或命令行结合 `--config`：
```bash
python review.py directory ./src --config .codereview.strict.yml
```
