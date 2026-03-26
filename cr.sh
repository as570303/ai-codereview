#!/usr/bin/env bash
# =============================================================================
# cr.sh — 基于 claude CLI 的代码审查脚本（复用 Claude Code 订阅，无需额外 API Key）
#
# 用法：
#   bash cr.sh file   <文件路径>
#   bash cr.sh dir    <目录路径>  [--max-files N]
#   bash cr.sh diff   [base]      如 HEAD~1 / main / abc123
#
# 依赖：claude CLI 已登录（claude --version 能正常输出即可）
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERROR]${RESET} $*" >&2; }

# ── 检查 claude CLI ────────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    error "未找到 claude 命令，请先安装 Claude Code CLI："
    echo "  https://claude.ai/code"
    exit 1
fi

# ── Prompt 模板 ────────────────────────────────────────────────────────────
SYSTEM_PROMPT='你是一位拥有 10 年经验的资深代码审查专家，精通安全、性能、代码质量和逻辑正确性。

## 审查维度（按优先级）
1. 安全（Security）：SQL 注入、XSS、硬编码密码/Token、权限绕过、敏感信息泄露
2. 逻辑（Logic）：空指针/空引用、边界条件、死循环、竞态条件、异常未捕获
3. 性能（Performance）：N+1 查询、不必要的循环嵌套、内存泄漏、重复计算
4. 质量（Quality）：命名不规范、函数过长（>50行）、重复代码、魔法数字

## 输出格式
用中文输出，每个问题包含：
- 【级别】Critical / High / Medium / Low
- 【文件】文件名 第 N 行
- 【问题】简短描述
- 【建议】具体修复方案（含代码示例）

如果没有发现问题，直接输出：✅ 未发现明显问题。
只报告真实存在的问题，不要推测或过度报告。'

# ── 工具函数 ────────────────────────────────────────────────────────────────
review_file() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        error "文件不存在：$file"
        exit 1
    fi

    local lang ext
    ext="${file##*.}"
    case "$ext" in
        py)   lang="Python" ;;
        js)   lang="JavaScript" ;;
        ts)   lang="TypeScript" ;;
        go)   lang="Go" ;;
        java) lang="Java" ;;
        rs)   lang="Rust" ;;
        rb)   lang="Ruby" ;;
        php)  lang="PHP" ;;
        *)    lang="代码" ;;
    esac

    echo -e "\n${BOLD}${CYAN}── 审查文件：$file ──${RESET}\n"

    local prompt="请审查以下 ${lang} 文件：${file}

\`\`\`${ext}
$(cat "$file")
\`\`\`"

    echo "$SYSTEM_PROMPT

$prompt" | claude -p --output-format text
}

review_directory() {
    local dir="${1:-.}"
    local max_files="${2:-20}"

    if [[ ! -d "$dir" ]]; then
        error "目录不存在：$dir"
        exit 1
    fi

    # 查找代码文件（排除常见无关目录）
    local files=()
    while IFS= read -r line; do
        files+=("$line")
    done < <(find "$dir" \
        -not \( -path "*/.git/*" -o -path "*/node_modules/*" \
             -o -path "*/.venv/*" -o -path "*/__pycache__/*" \
             -o -path "*/dist/*"  -o -path "*/build/*" \
             -o -path "*/vendor/*" \) \
        -type f \
        \( -name "*.py" -o -name "*.js" -o -name "*.ts" \
           -o -name "*.go" -o -name "*.java" -o -name "*.rs" \
           -o -name "*.rb" -o -name "*.php" -o -name "*.sh" \) \
        | sort | head -n "$max_files")

    if [[ ${#files[@]} -eq 0 ]]; then
        warn "目录 $dir 中未找到可审查的代码文件"
        exit 0
    fi

    echo -e "\n${BOLD}${CYAN}── 目录审查：$dir（共 ${#files[@]} 个文件）──${RESET}\n"

    local report_file="./code-review-report.md"
    {
        echo "# Code Review Report"
        echo ""
        echo "**审查目录**：\`$dir\`  "
        echo "**审查时间**：$(date '+%Y-%m-%d %H:%M')"
        echo "**文件数**：${#files[@]}"
        echo ""
        echo "---"
        echo ""
    } > "$report_file"

    local i=1
    for f in "${files[@]}"; do
        info "[$i/${#files[@]}] 审查：$f"
        local ext="${f##*.}"
        local result
        result=$(echo "$SYSTEM_PROMPT

请审查以下代码文件：${f}

\`\`\`${ext}
$(cat "$f")
\`\`\`" | claude -p --output-format text)

        {
            echo "## \`$f\`"
            echo ""
            echo "$result"
            echo ""
            echo "---"
            echo ""
        } >> "$report_file"

        echo -e "\n${result}\n"
        ((i++))
    done

    ok "报告已保存：$report_file"
}

review_diff() {
    local base="${1:-HEAD~1}"

    # 检查是否在 git 仓库
    if ! git rev-parse --git-dir &>/dev/null; then
        error "当前目录不是 Git 仓库：$(pwd)"
        echo "  请 cd 到你的项目目录再运行"
        exit 1
    fi

    # 获取变更文件列表
    local changed_files=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && changed_files+=("$line")
    done < <(git diff --name-only "$base" HEAD 2>/dev/null \
        | grep -E '\.(py|js|ts|go|java|rs|rb|php|sh|tsx|jsx|cs|cpp|c)$' || true)

    if [[ ${#changed_files[@]} -eq 0 ]]; then
        warn "与 $base 相比没有代码文件变更"
        exit 0
    fi

    echo -e "\n${BOLD}${CYAN}── Diff 审查：${base}..HEAD — ${#changed_files[@]} 个变更文件 ──${RESET}\n"

    local report_file="./code-review-report.md"
    {
        echo "# Code Review Report（Diff 模式）"
        echo ""
        echo "**基准**：\`${base}\`  "
        echo "**审查时间**：$(date '+%Y-%m-%d %H:%M')"
        echo "**变更文件数**：${#changed_files[@]}"
        echo ""
        echo "---"
        echo ""
    } > "$report_file"

    local i=1
    for f in "${changed_files[@]}"; do
        # 跳过已删除的文件
        [[ ! -f "$f" ]] && { info "跳过已删除文件：$f"; continue; }

        info "[$i/${#changed_files[@]}] 审查变更：$f"

        local diff_content
        diff_content=$(git diff -U20 "$base" HEAD -- "$f" 2>/dev/null)

        if [[ -z "$diff_content" ]]; then
            ((i++)); continue
        fi

        local result
        result=$(echo "$SYSTEM_PROMPT

请审查以下 Git diff（统一差异格式）。
文件路径：${f}
审查重点：'+' 开头的新增/变更行是本次改动，'-' 行是被删除的旧代码仅供上下文参考。
行号请基于新版本文件中的实际行号填写。

\`\`\`diff
${diff_content}
\`\`\`" | claude -p --output-format text)

        {
            echo "## \`$f\`"
            echo ""
            echo "$result"
            echo ""
            echo "---"
            echo ""
        } >> "$report_file"

        echo -e "\n${BOLD}$f${RESET}"
        echo -e "${result}\n"
        ((i++))
    done

    ok "审查完成，报告已保存：$report_file"
}

# ── 主入口 ──────────────────────────────────────────────────────────────────
CMD="${1:-}"

case "$CMD" in
    file)
        [[ -z "${2:-}" ]] && { error "用法：bash cr.sh file <文件路径>"; exit 1; }
        review_file "$2"
        ;;
    dir|directory)
        dir_path="${2:-.}"
        max_files=20
        # 解析 --max-files 参数
        shift; shift 2>/dev/null || true
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --max-files) max_files="${2:-20}"; shift 2 ;;
                *) shift ;;
            esac
        done
        review_directory "$dir_path" "$max_files"
        ;;
    diff)
        review_diff "${2:-HEAD~1}"
        ;;
    *)
        echo -e "${BOLD}用法：${RESET}"
        echo "  bash cr.sh file  <文件路径>              # 审查单个文件"
        echo "  bash cr.sh dir   <目录路径> [--max-files N]  # 审查整个目录"
        echo "  bash cr.sh diff  [base]                  # 审查 Git 变更（默认 HEAD~1）"
        echo ""
        echo -e "${BOLD}示例：${RESET}"
        echo "  bash cr.sh file src/app.py"
        echo "  bash cr.sh dir ./src --max-files 10"
        echo "  bash cr.sh diff HEAD~1"
        echo "  bash cr.sh diff main"
        ;;
esac
