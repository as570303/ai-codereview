#!/usr/bin/env bash
# =============================================================================
# build_mac.sh — 将 ai-codereview 编译为 macOS 平台专属 .whl（不含 Python 源码）
# 用法：bash build_mac.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info() { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()   { echo -e "${GREEN}[OK]${RESET}    $*"; }
err()  { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# ── 1. 激活虚拟环境 ──────────────────────────────────────────────────────────
if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
else
    err "未找到 .venv，请先运行：python -m venv .venv && pip install -r requirements.txt"
fi

# ── 2. 安装构建依赖 ──────────────────────────────────────────────────────────
info "检查构建依赖..."
pip install cython wheel setuptools --upgrade -q
ok "构建依赖就绪"

# ── 3. 清理上次产物 ──────────────────────────────────────────────────────────
info "清理旧产物..."
rm -rf build/ dist/cython/
# 清理 Cython 生成的 .c 中间文件和 .so
find . -maxdepth 1 -name "*.c"  -delete
find . -maxdepth 1 -name "*.so" -delete
ok "清理完成"

# ── 4. 编译 .so ──────────────────────────────────────────────────────────────
info "Cython 编译中（并行 4 线程）..."
python setup_cython.py build_ext --inplace 2>&1 | grep -E "(Cythonizing|building|error|Error)" || true

# 检查关键模块是否生成
if ! ls review.cpython-*.so &>/dev/null; then
    err "编译失败：未找到 review.cpython-*.so"
fi
ok "编译完成，生成 .so 文件："
ls -lh ./*.so 2>/dev/null | awk '{print "  " $5 "  " $9}'

# ── 5. 打包 wheel ────────────────────────────────────────────────────────────
info "打包 wheel（仅含 .so，不含 .py 源码）..."
mkdir -p dist/cython
python setup_cython.py bdist_wheel --dist-dir dist/cython 2>&1 | grep -E "(created|copying|error)" || true

WHL=$(ls dist/cython/*.whl 2>/dev/null | head -1)
if [[ -z "$WHL" ]]; then
    err "wheel 打包失败"
fi

ok "wheel 构建成功："
echo -e "  ${BOLD}${WHL}${RESET}  ($(du -sh "$WHL" | cut -f1))"

# ── 6. 从 wheel 中剔除 .py 源码 ─────────────────────────────────────────────
info "从 wheel 中剔除 .py 源码文件..."
python - "$WHL" <<'PYEOF'
import sys, os, zipfile

# review.py 是 CLI 入口（Typer 需要 inspect 反射），保留为纯 Python
KEEP_PY = {"review"}
COMPILED = {"baseline","config","eval","llm_client","parser","preprocessor","prompts","tools"}

whl = sys.argv[1]
tmp = whl + ".tmp"
with zipfile.ZipFile(whl, "r") as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
    removed = []
    for item in src.infolist():
        name = item.filename
        stem = name.split(".")[0]
        if name.endswith(".py") and stem in COMPILED and "/" not in name:
            removed.append(name)
            continue
        dst.writestr(item, src.read(name))
os.replace(tmp, whl)

with zipfile.ZipFile(whl) as z:
    names = z.namelist()
    so = [n for n in names if n.endswith(".so")]
    py = [n for n in names if n.endswith(".py") and "/" not in n]
    print(f"  .so 二进制：{len(so)} 个，.py 保留：{py}")
    if any(stem not in KEEP_PY for stem in (n.split(".")[0] for n in py)):
        print("  警告：存在未预期的 .py 文件", file=sys.stderr)
        sys.exit(1)
PYEOF

# ── 7. 清理 .c 中间文件 ──────────────────────────────────────────────────────
info "清理 Cython .c 中间文件..."
find . -maxdepth 1 -name "*.c" -delete
ok "清理完成"

echo ""
echo -e "${BOLD}${GREEN}构建完成！${RESET}"
echo ""
echo -e "${BOLD}安装方式：${RESET}"
echo "  pip install $WHL"
echo "  pip install \"$WHL[git]\"  # 含 Git diff 功能"
echo ""
echo -e "${BOLD}验证安装：${RESET}"
echo "  codereview --help"
