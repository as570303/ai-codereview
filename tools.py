from __future__ import annotations

import ast
import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import git

logger = logging.getLogger("codereview")

# 单文件最大读取字节数默认值（可通过 ReviewConfig.max_file_size_kb 覆盖）
_DEFAULT_MAX_FILE_SIZE_BYTES = 512 * 1024  # 512 KB

# 语言扩展名映射
_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".c": "c",
    ".rs": "rust",
    ".swift": "swift",
    ".kt": "kotlin",
    ".sql": "sql",
    ".sh": "shell",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".tf": "hcl",
    ".hcl": "hcl",
}

# 每块最大字符数估算（1 token ≈ 4 chars，目标 3000 tokens/chunk）
MAX_CHUNK_CHARS = 12_000
# preamble 最大行数：防止大量 import 对每个函数 chunk 重复发送，浪费 token
_MAX_PREAMBLE_LINES = 30
# 启发式切块最大 chunk 数：防止超大文件产生数百次 API 调用
_MAX_HEURISTIC_CHUNKS = 100
# 启发式切块每块行数及重叠行数（1 token ≈ 4 chars，150 行 ≈ 2000 tokens 留余量）
_HEURISTIC_CHUNK_LINES = 150
_HEURISTIC_OVERLAP_LINES = 20


CHUNK_TYPE_DIFF = "diff"


@dataclass
class CodeChunk:
    name: str           # 函数/类名，或 "module_level"
    chunk_type: str     # "function" | "class" | "module" | "diff" | "heuristic"
    line_start: int
    line_end: int
    code: str


def detect_language(file_path: str) -> str:
    return _EXT_MAP.get(Path(file_path).suffix.lower(), "unknown")


def read_file(file_path: str, max_size_bytes: int = _DEFAULT_MAX_FILE_SIZE_BYTES) -> str:
    # 先读取再校验大小，消除 stat() 与 read_bytes() 之间的 TOCTOU 竞态条件
    raw = Path(file_path).read_bytes()
    if len(raw) > max_size_bytes:
        raise ValueError(
            f"文件过大（{len(raw) // 1024} KB > {max_size_bytes // 1024} KB 限制），"
            f"跳过审查：{file_path}。"
            f"可在 .codereview.yml 中设置 max_file_size_kb 调整上限。"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("文件含非 UTF-8 字节，已用替换字符处理（可能影响审查准确性）：%s", file_path)
        return raw.decode("utf-8", errors="replace")


def should_ignore(file_path: str, ignore_patterns: list[str]) -> bool:
    p = Path(file_path)
    for pattern in ignore_patterns:
        if fnmatch.fnmatch(str(p), pattern):
            return True
        if fnmatch.fnmatch(p.name, pattern):
            return True
        for part in p.parts:
            if fnmatch.fnmatch(part + "/", pattern) or fnmatch.fnmatch(part, pattern.rstrip("/")):
                return True
    return False


def list_code_files(directory: str, ignore_patterns: list[str]) -> list[str]:
    # os.walk + 原地目录剪枝：跳过 .git / 符号链接目录 / 匹配 ignore 的目录，
    # 避免 rglob("*") 在含 node_modules、vendor 等大目录的仓库中枚举数万个无关文件。
    files = []
    for dirpath, dirnames, filenames in os.walk(directory):
        # 原地修改 dirnames，os.walk 将不再递归被移除的目录
        dirnames[:] = sorted(
            d for d in dirnames
            if d != ".git"
            and not Path(dirpath, d).is_symlink()
            and (not ignore_patterns or not should_ignore(os.path.join(dirpath, d), ignore_patterns))
        )
        for filename in sorted(filenames):
            fp = os.path.join(dirpath, filename)
            path = Path(fp)
            if path.is_symlink():
                continue
            if path.suffix.lower() not in _EXT_MAP:
                continue
            if ignore_patterns and should_ignore(fp, ignore_patterns):
                continue
            files.append(fp)
    return sorted(files)


def chunk_file(file_path: str, source: str, language: str) -> list[CodeChunk]:
    """按函数/类切块。Python 用 AST，其他语言降级到启发式切块。"""
    if language.lower() == "python":
        return _chunk_python(source, file_path)
    return _chunk_heuristic(source)


_AST_TYPE_MAP = {
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "async_function",
    ast.ClassDef: "class",
}

# RHS 节点类型白名单：只有这些类型的全局赋值才纳入 preamble（纯字面量常量）
_LITERAL_TYPES = (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.JoinedStr)


def _chunk_python(source: str, file_path: str = "") -> list[CodeChunk]:
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.debug("Python 语法错误，降级为启发式切块：%s", file_path or "<source>")
        return _chunk_heuristic(source)

    # 收集顶层 import + 字面量常量赋值作为上下文前缀。
    # 注意：只有 RHS 为字面量（_LITERAL_TYPES）的赋值才算 preamble；
    # 函数调用赋值（如 result = get_config()）保留在 module_level 中正常审查。
    preamble_lines: list[int] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            preamble_lines.extend(range(node.lineno, node.end_lineno + 1))
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, _LITERAL_TYPES):
                preamble_lines.extend(range(node.lineno, node.end_lineno + 1))
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and isinstance(node.value, _LITERAL_TYPES):
                preamble_lines.extend(range(node.lineno, node.end_lineno + 1))

    preamble_raw = "\n".join(lines[i - 1] for i in sorted(set(preamble_lines)) if i <= len(lines))
    preamble_split = preamble_raw.splitlines()
    if len(preamble_split) > _MAX_PREAMBLE_LINES:
        preamble = "\n".join(preamble_split[:_MAX_PREAMBLE_LINES]) + "\n# ... (preamble truncated)"
    else:
        preamble = preamble_raw

    chunks: list[CodeChunk] = []
    top_level = [n for n in ast.iter_child_nodes(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]

    if not top_level:
        return _chunk_heuristic(source)

    for node in top_level:
        start = node.lineno
        end = node.end_lineno
        snippet = "\n".join(lines[start - 1:end])
        # 附加 preamble（import 上下文），避免 LLM 因缺少上下文误判
        if preamble:
            full = (
                f"# [imports/globals]\n{preamble}\n\n"
                f"# [function/class, 文件实际行号从第 {start} 行开始]\n{snippet}"
            )
        else:
            full = snippet
        if len(full) > MAX_CHUNK_CHARS:
            # 函数/类体超出上下文限制，降级为启发式子切块
            sub_chunks = _chunk_heuristic(snippet)
            for sc in sub_chunks:
                sc.name = f"{node.name}/{sc.name}"
                sc.line_start = start + sc.line_start - 1
                sc.line_end = start + sc.line_end - 1
            chunks.extend(sub_chunks)
        else:
            chunks.append(CodeChunk(
                name=node.name,
                chunk_type=_AST_TYPE_MAP.get(type(node), "unknown"),
                line_start=start,
                line_end=end,
                code=full,
            ))

    # 模块级代码（不属于任何函数/类的语句）
    defined_lines: set[int] = set()
    for node in top_level:
        defined_lines.update(range(node.lineno, node.end_lineno + 1))

    # 排除 preamble 行：import/全局变量已作为上下文前缀发给每个函数 chunk，不必重复审查
    preamble_set = set(preamble_lines)
    module_line_indices = [i for i in range(len(lines))
                           if (i + 1) not in defined_lines and (i + 1) not in preamble_set]
    module_code = "\n".join(lines[i] for i in module_line_indices).strip()
    if module_code:
        # 用模块级语句中实际最小/最大行号，避免"1 到文件末尾"的误导性范围
        actual_start = module_line_indices[0] + 1 if module_line_indices else 1
        actual_end   = module_line_indices[-1] + 1 if module_line_indices else len(lines)
        chunks.append(CodeChunk(
            name="module_level",
            chunk_type="module",
            line_start=actual_start,
            line_end=actual_end,
            code=module_code,
        ))

    return chunks or _chunk_heuristic(source)


def _chunk_heuristic(source: str) -> list[CodeChunk]:
    """按固定行数切块，前后保留重叠行。超过 _MAX_HEURISTIC_CHUNKS 时截断并记录警告。"""
    lines = source.splitlines()
    if not lines:
        return []

    chunks: list[CodeChunk] = []
    i = 0
    idx = 1

    while i < len(lines):
        if len(chunks) >= _MAX_HEURISTIC_CHUNKS:
            logger.warning(
                "文件过长，启发式切块已达上限 %d，剩余约 %d 行跳过审查（建议拆分文件）",
                _MAX_HEURISTIC_CHUNKS, len(lines) - i,
            )
            break
        end = min(i + _HEURISTIC_CHUNK_LINES, len(lines))
        snippet = "\n".join(lines[i:end])
        chunks.append(CodeChunk(
            name=f"chunk_{idx}",
            chunk_type="heuristic",
            line_start=i + 1,
            line_end=end,
            code=snippet,
        ))
        i += _HEURISTIC_CHUNK_LINES - _HEURISTIC_OVERLAP_LINES
        idx += 1

    return chunks


_HUNK_PATTERN = re.compile(r"^@@[^@]+@@", re.MULTILINE)
# 从 @@ -a,b +c,d @@ 提取新版本起始行号 c
_HUNK_NEW_LINE_RE = re.compile(r"\+(\d+)")
# 提取 @@ -a,b +c,d @@ 中的新版本范围（start, count）
_HUNK_RANGE_RE = re.compile(r'\+(\d+)(?:,(\d+))?')


def _parse_hunk_line_start(hunk: str) -> int:
    """从 unified diff hunk 头提取新版本文件的起始行号。"""
    first_line = hunk.split("\n", 1)[0]
    m = _HUNK_NEW_LINE_RE.search(first_line)
    return int(m.group(1)) if m else 1


def chunk_diff(diff_text: str) -> list[CodeChunk]:
    """将 unified diff 文本切成 chunk（按 hunk 分段，超长时合并）。"""
    if not diff_text.strip():
        return []

    # 按 @@ hunk 头切分
    positions = [m.start() for m in _HUNK_PATTERN.finditer(diff_text)]

    if not positions:
        return [CodeChunk(name="diff_changes", chunk_type=CHUNK_TYPE_DIFF,
                          line_start=1, line_end=len(diff_text.splitlines()), code=diff_text)]

    chunks: list[CodeChunk] = []
    positions.append(len(diff_text))
    current_code = ""
    current_start = 1
    idx = 1

    for i, start in enumerate(positions[:-1]):
        end = positions[i + 1]
        hunk = diff_text[start:end]
        if len(current_code) + len(hunk) > MAX_CHUNK_CHARS and current_code:
            chunks.append(CodeChunk(
                name=f"diff_hunk_{idx}", chunk_type=CHUNK_TYPE_DIFF,
                line_start=current_start,
                line_end=max_line_from_diff(current_code),
                code=current_code,
            ))
            idx += 1
            current_code = hunk
            current_start = _parse_hunk_line_start(hunk)
        else:
            if not current_code:
                current_start = _parse_hunk_line_start(hunk)
            current_code += hunk

    if current_code:
        chunks.append(CodeChunk(
            name=f"diff_hunk_{idx}", chunk_type=CHUNK_TYPE_DIFF,
            line_start=current_start,
            line_end=max_line_from_diff(current_code),
            code=current_code,
        ))

    return chunks


def is_code_file(file_path: str) -> bool:
    """判断文件是否为支持审查的代码文件。"""
    return Path(file_path).suffix.lower() in _EXT_MAP


def max_line_from_diff(diff_text: str) -> int:
    """从 unified diff hunk 头推断新版本文件的最大行号，用于 line_verified 校验。
    比硬编码 9999 更准确，避免将所有 LLM 报告行号误标记为已验证。
    返回所有 hunk 中 new_start + new_count 的最大值，至少为 1。
    """
    max_line = 1
    for m in _HUNK_PATTERN.finditer(diff_text):
        rng = _HUNK_RANGE_RE.search(m.group(0))
        if rng:
            start = int(rng.group(1))
            count = int(rng.group(2)) if rng.group(2) is not None else 1
            max_line = max(max_line, start + count - 1)
    return max_line


def get_file_diff(repo: git.Repo, base: str, file_path: str, context_lines: int = 20) -> str:
    """提取指定文件相对于 base 的 unified diff（含 context 行）。"""
    try:
        return repo.git.diff(f"-U{context_lines}", base, "HEAD", "--", file_path)
    except Exception as e:
        logger.warning("git diff 失败 file=%s base=%s：%s，将退回全文件审查", file_path, base, e)
        return ""
