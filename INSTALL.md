# 安装指南

## 环境要求

- macOS / Linux / Windows
- Python >= 3.11
- （可选）claude CLI — 使用 `--use-cli` 模式时需要，无需额外 API Key

---

## 方式一：pipx 安装（推荐，命令行工具首选）

pipx 会自动管理虚拟环境，安装后 `codereview` 命令全局可用。

```bash
# 1. 安装 pipx（已有可跳过）
brew install pipx        # macOS
pipx ensurepath

# 2. 安装 ai-codereview
pipx install "git+https://github.com/as570303/ai-codereview.git"

# 3. 注入 Git diff 依赖（需要 codereview diff 功能时）
pipx inject ai-codereview gitpython

# 4. 验证
codereview --help
```

---

## 方式二：虚拟环境安装

```bash
# 1. 创建虚拟环境
python3 -m venv ~/ai-codereview-env
source ~/ai-codereview-env/bin/activate

# 2. 安装
pip install "git+https://github.com/as570303/ai-codereview.git[git]"

# 3. 验证
codereview --help
```

每次使用前需激活虚拟环境：

```bash
source ~/ai-codereview-env/bin/activate
```

如需自动激活，将以下内容加入 `~/.zshrc`：

```bash
echo 'source ~/ai-codereview-env/bin/activate' >> ~/.zshrc
```

---

## 方式三：克隆源码安装

```bash
git clone https://github.com/as570303/ai-codereview.git
cd ai-codereview
bash quickstart.sh   # 自动完成：检查环境 → 安装依赖 → 配置 API Key → 运行演示
```

---

## 配置 API Key

安装完成后，在项目目录创建 `.env` 文件：

```bash
echo "ANTHROPIC_API_KEY=sk-ant-xxxx" > .env
```

> 如果使用 `--use-cli` 模式（复用 Claude Code 订阅），无需配置 API Key，确保 `claude` 命令可用即可。

---

## 快速验证

```bash
# 进入任意有代码的项目目录
cd /path/to/your/project

# 审查单个文件（使用 Claude Code 订阅）
codereview file src/app.py --use-cli

# 审查本次 Git 变更
codereview diff HEAD~1 --use-cli
```

---

## 常用命令

```bash
# 审查单个文件
codereview file src/app.py --use-cli

# 审查整个目录
codereview directory ./src --use-cli

# 审查最近一次提交的变更
codereview diff HEAD~1 --use-cli

# 审查未提交的变更（含已暂存 + 未暂存）
codereview diff UNCOMMITTED --use-cli

# 审查与主分支的差异
codereview diff main --use-cli

# 审查指定 commit
codereview diff abc123 --use-cli

# 只报告 High 及以上问题
codereview diff HEAD~1 --use-cli --config .codereview.yml
# （在 .codereview.yml 中设置 severity_threshold: high）

# 指定报告输出路径
codereview diff HEAD~1 --use-cli --output ./reports/review.md
```

| 命令 | 说明 |
|------|------|
| `codereview file <文件>` | 审查单个文件 |
| `codereview directory <目录>` | 审查整个目录（并发） |
| `codereview diff UNCOMMITTED` | 审查未提交的变更 |
| `codereview diff HEAD~1` | 审查最近一次提交 |
| `codereview diff main` | 审查与主分支的差异 |

---

## 更新到最新版

```bash
# pipx 方式（推荐：卸载重装确保获取最新代码）
pipx uninstall ai-codereview
pipx install "git+https://github.com/as570303/ai-codereview.git"
pipx inject ai-codereview gitpython

# 虚拟环境方式
pip install --upgrade "git+https://github.com/as570303/ai-codereview.git[git]"
```

---

## 卸载

```bash
# pipx 方式
pipx uninstall ai-codereview

# 虚拟环境方式
pip uninstall ai-codereview
```

---

## 常见问题

**`zsh: command not found: pip`**
```bash
# 用 pip3 或 pipx 替代
pip3 install ...
```

**`error: externally-managed-environment`**
```bash
# 用 pipx（推荐）或虚拟环境安装，不要直接安装到系统 Python
brew install pipx && pipx install "git+https://github.com/as570303/ai-codereview.git"
```

**`ModuleNotFoundError: No module named 'baseline'`**
```bash
# 重新安装最新版
pipx uninstall ai-codereview
pipx install "git+https://github.com/as570303/ai-codereview.git"
pipx inject ai-codereview gitpython
```

**`当前目录不是 Git 仓库`**
```bash
# diff 命令需要在 git 仓库目录下执行
cd /path/to/your/git/project
codereview diff HEAD~1 --use-cli
```
