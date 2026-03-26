#!/usr/bin/env bash
# =============================================================================
# ai-codereview 快速上手脚本
# 用法：bash quickstart.sh
# =============================================================================
set -euo pipefail

# ── 颜色 ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
header()  { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════${RESET}"; \
            echo -e "${BOLD}${CYAN}  $*${RESET}"; \
            echo -e "${BOLD}${CYAN}══════════════════════════════════════${RESET}"; }

# ── 脚本所在目录 ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# =============================================================================
# 步骤 1：检查 Python 版本
# =============================================================================
header "步骤 1 / 6  检查 Python 环境"

PYTHON=""
for cmd in python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        # 用 Python 本身做版本判断，避免 bash 字符串解析兼容问题
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
            ver=$("$cmd" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
            PYTHON="$cmd"
            ok "找到 Python ${ver}：$(command -v $cmd)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    error "需要 Python >= 3.11，未找到合适版本。"
    echo "  macOS:  brew install python@3.12"
    echo "  Ubuntu: sudo apt install python3.12"
    exit 1
fi

# =============================================================================
# 步骤 2：创建虚拟环境
# =============================================================================
header "步骤 2 / 6  配置虚拟环境"

VENV_DIR="$SCRIPT_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
    ok "虚拟环境已存在：$VENV_DIR"
else
    info "创建虚拟环境 $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "虚拟环境创建完成"
fi

# 激活虚拟环境
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
PYTHON="python"   # 后续统一用 venv 内的 python
ok "虚拟环境已激活"

# =============================================================================
# 步骤 3：安装依赖
# =============================================================================
header "步骤 3 / 6  安装依赖"

info "升级 pip ..."
"$PYTHON" -m pip install --upgrade pip -q

info "安装项目依赖 ..."
"$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt" -q
ok "依赖安装完成"

# =============================================================================
# 步骤 4：配置 API Key
# =============================================================================
header "步骤 4 / 6  配置 API Key"

ENV_FILE="$SCRIPT_DIR/.env"

# 优先读取已有 .env
if [[ -f "$ENV_FILE" ]]; then
    existing_key=$(grep -E '^ANTHROPIC_API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d ' ')
    if [[ -n "$existing_key" && ${#existing_key} -gt 10 ]]; then
        ok ".env 文件已存在，API Key 已配置（${existing_key:0:12}...）"
        USE_LOCAL=false
    else
        warn ".env 文件存在但 ANTHROPIC_API_KEY 为空"
        existing_key=""
    fi
fi

# 环境变量中已设置
if [[ -z "${existing_key:-}" && -n "${ANTHROPIC_API_KEY:-}" ]]; then
    ok "检测到环境变量 ANTHROPIC_API_KEY（${ANTHROPIC_API_KEY:0:12}...）"
    echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" > "$ENV_FILE"
    USE_LOCAL=false
fi

# 需要用户输入
if [[ -z "${existing_key:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo ""
    echo -e "  请输入 Anthropic API Key（以 ${BOLD}sk-ant-${RESET} 开头）"
    echo -e "  获取地址：https://console.anthropic.com/settings/keys"
    echo -e "  ${YELLOW}（直接回车跳过，将使用本地 Ollama 模式）${RESET}"
    echo ""
    if [[ -t 0 ]]; then
        read -rp "  API Key > " api_key
    else
        api_key=""
        info "非交互式环境，跳过 API Key 输入"
    fi
    api_key=$(echo "$api_key" | tr -d ' ')

    if [[ -n "$api_key" ]]; then
        echo "ANTHROPIC_API_KEY=${api_key}" > "$ENV_FILE"
        ok "API Key 已写入 .env"
        USE_LOCAL=false
    else
        warn "跳过 API Key 配置，将使用本地 Ollama 模式"
        USE_LOCAL=true
    fi
fi

# =============================================================================
# 步骤 5：生成示例配置文件
# =============================================================================
header "步骤 5 / 6  生成配置文件"

CONFIG_FILE="$SCRIPT_DIR/.codereview.yml"
if [[ -f "$CONFIG_FILE" ]]; then
    ok "配置文件已存在：${CONFIG_FILE}（跳过生成）"
else
    info "生成默认配置文件 $CONFIG_FILE ..."

    if [[ "${USE_LOCAL:-false}" == "true" ]]; then
        USE_LOCAL_VAL="true"
    else
        USE_LOCAL_VAL="false"
    fi

    cat > "$CONFIG_FILE" <<YAML
# ai-codereview 配置文件
# 完整说明见 USAGE.md

# ── 模型 ─────────────────────────────────────────────────────────────────────
model: claude-sonnet-4-6        # 可选：claude-opus-4-6（最强）/ claude-haiku-4-5（最快）
temperature: 0.0
max_output_tokens: 8192

# ── 审查行为 ──────────────────────────────────────────────────────────────────
language: auto                  # auto = 自动检测文件语言
severity_threshold: low         # 报告所有级别：critical / high / medium / low
desensitize: true               # 自动脱敏 API Key / 密码 / Token
max_file_size_kb: 512           # 超过此大小的文件跳过

# ── 忽略路径 ──────────────────────────────────────────────────────────────────
ignore_paths:
  - "*.pyc"
  - "__pycache__"
  - "node_modules"
  - "vendor"
  - ".venv"
  - "dist"
  - "build"

# ── 输出 ─────────────────────────────────────────────────────────────────────
output:
  formats: [markdown]           # 可加 sarif 同时输出 GitHub Code Scanning 格式
  report_path: ./code-review-report.md

# ── 并发与限速 ────────────────────────────────────────────────────────────────
concurrency:
  max_workers: 5
  rate_limit_rpm: 50

# ── 重试 ─────────────────────────────────────────────────────────────────────
retry:
  max_attempts: 3
  timeout: 60.0

# ── 本地 Ollama 模型（可选）──────────────────────────────────────────────────
use_local: ${USE_LOCAL_VAL}
local_base_url: http://localhost:11434/v1
local_model: codellama
YAML
    ok "配置文件已生成：$CONFIG_FILE"
fi

# =============================================================================
# 步骤 6：运行演示
# =============================================================================
header "步骤 6 / 6  运行演示审查"

DEMO_FILE="$SCRIPT_DIR/golden_dataset/security/sql_injection.py"

if [[ ! -f "$DEMO_FILE" ]]; then
    warn "演示文件不存在：$DEMO_FILE，跳过演示"
else
    echo ""
    echo -e "  将审查演示文件：${BOLD}golden_dataset/security/sql_injection.py${RESET}"
    echo -e "  该文件包含典型 SQL 注入漏洞，AI 应能正确识别。"
    echo ""
    # 非交互式环境（CI / 管道）自动跳过演示
    if [[ -t 0 ]]; then
        read -rp "  按回车开始演示，输入 s 跳过 > " choice
    else
        choice="s"
        info "非交互式环境，自动跳过演示"
    fi
    choice=$(echo "${choice:-}" | tr '[:upper:]' '[:lower:]')

    if [[ "$choice" == "s" ]]; then
        info "已跳过演示"
    else
        echo ""
        REPORT_PATH="$SCRIPT_DIR/demo-review-report.md"

        if [[ "${USE_LOCAL:-false}" == "true" ]]; then
            # 检查 Ollama 是否运行
            if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
                warn "Ollama 未运行，请先执行：ollama serve"
                warn "然后拉取模型：ollama pull codellama"
                warn "跳过演示"
            else
                "$PYTHON" "$SCRIPT_DIR/review.py" file "$DEMO_FILE" \
                    --config "$CONFIG_FILE" \
                    --output "$REPORT_PATH" \
                    --local
            fi
        else
            "$PYTHON" "$SCRIPT_DIR/review.py" file "$DEMO_FILE" \
                --config "$CONFIG_FILE" \
                --output "$REPORT_PATH" || true
        fi

        if [[ -f "$REPORT_PATH" ]]; then
            echo ""
            ok "演示报告已保存：$REPORT_PATH"
            echo ""
            echo -e "${BOLD}报告预览（前 40 行）：${RESET}"
            echo "──────────────────────────────────────"
            head -40 "$REPORT_PATH"
            echo "──────────────────────────────────────"
        fi
    fi
fi

# =============================================================================
# 完成提示
# =============================================================================
header "设置完成！"

echo -e "
${BOLD}激活虚拟环境：${RESET}
  source .venv/bin/activate

${BOLD}常用命令：${RESET}
  # 审查单个文件
  python review.py file <文件路径>

  # 审查整个目录（最多 50 个文件）
  python review.py directory ./src

  # 只审查本次 Git 变更（推荐用于 CR 流程）
  python review.py diff HEAD~1

  # 只报告 High 及以上问题
  python review.py directory ./src --config .codereview.yml
  # 修改 .codereview.yml 中 severity_threshold: high

  # 增量模式：只报告新增问题（需先建立基线）
  python review.py directory ./src --update-baseline   # 第一次建立基线
  python review.py diff main --incremental             # 后续只看新增

${BOLD}配置文件：${RESET}  .codereview.yml
${BOLD}完整文档：${RESET}  USAGE.md
"
