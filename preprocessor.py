from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("codereview")

# 敏感信息匹配规则：(pattern, replacement)
_PATTERNS: list[tuple[str, str]] = [
    # 赋值语句中的 key/secret/password/token
    (
        r'(?i)((?:api[_-]?key|secret(?:[_-]?key)?|password|passwd|token|auth[_-]?token'
        r'|access[_-]?key|private[_-]?key)\s*[=:]\s*)["\']([^"\']{6,})["\']',
        r"\1\"***REDACTED***\"",
    ),
    # 环境变量赋值 export FOO=bar
    (
        r'(?i)(export\s+(?:API_KEY|SECRET|PASSWORD|TOKEN|AUTH)\s*=\s*)(\S+)',
        r"\1***REDACTED***",
    ),
    # 看起来像 Bearer token
    (r'Bearer\s+[A-Za-z0-9\-._~+/=]{20,}', "Bearer ***REDACTED***"),
    # AWS Access Key ID
    (r'\b(AKIA[0-9A-Z]{16})\b', "***AWS_KEY_REDACTED***"),
    # 赋值语句中长度 ≥ 32 的 hex 字符串（需要赋值上下文，减少误报）
    (
        r'(?i)((?:secret|key|token|password|passwd|salt|hash)\s*[=:]\s*["\']?)([0-9a-fA-F]{32,64})(["\']?)',
        r"\1***HEX_REDACTED***\3",
    ),
]

_COMPILED = [(re.compile(p), r) for p, r in _PATTERNS]


@dataclass
class PreprocessResult:
    code: str
    redacted_count: int


_MAX_LINE_LEN = 2000  # 超出此长度的单行跳过正则匹配，防止复杂模式对超长行发生灾难性回溯


def _safe_line(line: str) -> str:
    """超长行截断处理，防止正则 ReDoS。
    - 注释风格标记确保 LLM 不会将截断符误解为代码语法，避免产生误报。
    - 保留原始行尾（\\n / \\r\\n），防止 "".join() 后相邻行合并。
    """
    if len(line) <= _MAX_LINE_LEN:
        return line
    stripped = line.rstrip("\r\n")
    ending = line[len(stripped):]  # 原始行尾（"\n"、"\r\n" 或 ""）
    return stripped[:_MAX_LINE_LEN] + "  # [CODEREVIEW: LINE TRUNCATED]" + ending


def preprocess(code: str, enabled: bool = True) -> PreprocessResult:
    if not enabled:
        return PreprocessResult(code=code, redacted_count=0)

    # 逐行处理：对超长行跳过复杂 regex，只做截断保护
    safe_lines = [_safe_line(ln) for ln in code.splitlines(keepends=True)]
    result = "".join(safe_lines)
    total = 0

    for pattern, replacement in _COMPILED:
        new, count = pattern.subn(replacement, result)
        if count > 0:
            result = new
            total += count
            logger.debug("脱敏：匹配 %d 处（pattern: %.40s...）", count, pattern.pattern)

    return PreprocessResult(code=result, redacted_count=total)
