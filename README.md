# ai-codereview

基于 Anthropic Claude 的多维度 AI 代码审查工具，支持单文件、整目录、Git diff 三种模式，覆盖安全、逻辑、性能、质量四个审查维度。

## 安装

```bash
# 从 GitHub 安装（推荐）
pip install "git+https://github.com/as570303/ai-codereview.git[git]"

# 安装指定版本
pip install "git+https://github.com/as570303/ai-codereview.git@v1.0.0[git]"

# 本地克隆安装
git clone https://github.com/as570303/ai-codereview.git
cd ai-codereview
pip install -e ".[git]"
```

## 快速开始

```bash
# 配置 API Key
echo "ANTHROPIC_API_KEY=sk-ant-xxxx" > .env

# 审查单个文件
codereview file src/app.py

# 审查整个目录
codereview directory ./src

# 只审查本次 Git 变更
codereview diff HEAD~1

# 使用 Claude Code 订阅额度（无需 API Key）
codereview diff HEAD~1 --use-cli
```

## 主要特性

- **四维审查**：安全（SQL 注入/XSS/硬编码密钥）、逻辑（空指针/边界/竞态）、性能（N+1/内存泄漏）、质量（命名/复杂度）
- **多种模式**：单文件 / 整目录（并发）/ Git diff（只看变更）
- **结构化输出**：Markdown 报告 + SARIF（供 GitHub Code Scanning 消费）
- **增量基线**：过滤已知问题，CI 中只报告新引入的问题
- **自定义规则**：通过 Markdown 文件为 LLM 补充团队编码规范
- **本地模型**：支持 Ollama（codellama / deepseek-coder 等）
- **CLI 模式**：`--use-cli` 通过 claude 子进程调用，复用 Claude Code 订阅额度

## 配置

工具默认读取当前目录的 `.codereview.yml`：

```yaml
model: claude-sonnet-4-6
severity_threshold: low      # critical / high / medium / low
ignore_paths:
  - "*.pyc"
  - "node_modules"
  - ".venv"
output:
  formats: [markdown, sarif]
  report_path: ./code-review-report.md
concurrency:
  max_workers: 5
  rate_limit_rpm: 50
```

完整配置说明见 [USAGE.md](USAGE.md)。

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 审查完成，无 Critical 问题 |
| `1` | 发生错误 |
| `2` | 发现 Critical 问题（适合 CI 阻断合并） |

## GitHub Actions 集成

```yaml
- name: AI Code Review
  run: |
    pip install "ai-codereview[git]"
    codereview diff ${{ github.base_ref }}
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## License

MIT
