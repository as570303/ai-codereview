from __future__ import annotations

import copy
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompts import PROMPT_VERSION

if TYPE_CHECKING:
    from config import ScoringConfig

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
_SARIF_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}


@dataclass
class Issue:
    id: str
    dimension: str
    severity: str
    file: str
    line_start: int
    line_end: int
    line_verified: bool
    title: str
    description: str
    suggestion: str
    reference: str = ""


@dataclass
class ReviewResult:
    file: str
    language: str
    issues: list[Issue] = field(default_factory=list)
    reviewed_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # 使用 prompts.PROMPT_VERSION 而非重复读取环境变量，保证与渲染函数所用版本一致
    prompt_version: str = field(default_factory=lambda: PROMPT_VERSION)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def parse_llm_response(raw: dict[str, Any], file_path: str, total_lines: int) -> list[Issue]:
    issues = []
    for item in raw.get("issues", []):
        line_start = item.get("line_start", 0)
        line_end = item.get("line_end", line_start)
        line_verified = (1 <= line_start <= total_lines) and (1 <= line_end <= total_lines)

        issues.append(Issue(
            id=item.get("id", "UNKNOWN"),
            dimension=item.get("dimension", "quality").lower(),
            severity=item.get("severity", "low").lower(),
            file=file_path,
            line_start=line_start,
            line_end=line_end,
            line_verified=line_verified,
            title=item.get("title", ""),
            description=item.get("description", ""),
            suggestion=item.get("suggestion", ""),
            reference=item.get("reference", ""),
        ))
    return issues


def calculate_score(issues: list[Issue], scoring: ScoringConfig) -> int:
    deductions = 0
    for issue in issues:
        if issue.severity == "critical":
            deductions += scoring.critical_weight
        elif issue.severity == "high":
            deductions += scoring.high_weight
        elif issue.severity == "medium":
            deductions += scoring.medium_weight
        elif issue.severity == "low":
            deductions += scoring.low_weight
    return max(0, 100 - deductions)


def filter_by_threshold(issues: list[Issue], threshold: str) -> list[Issue]:
    threshold_level = SEVERITY_ORDER.get(threshold, 3)
    return [i for i in issues if SEVERITY_ORDER.get(i.severity, 3) <= threshold_level]


_DIM_PREFIX = {"security": "SEC", "logic": "LOGIC", "performance": "PERF", "quality": "QUAL"}


def renumber_issue_ids(issues: list[Issue]) -> None:
    """对所有 Issue 按维度全局重新编号（原地修改），确保跨 chunk / 跨文件 ID 唯一。
    LLM 各自独立生成 ID（都叫 SEC-001），合并后必须重新分配。
    无返回值——调用方不应依赖返回值，直接修改传入列表。
    """
    counters: dict[str, int] = {}
    for issue in issues:
        prefix = _DIM_PREFIX.get(issue.dimension, "UNK")
        counters[prefix] = counters.get(prefix, 0) + 1
        issue.id = f"{prefix}-{counters[prefix]:03d}"


def deduplicate(issues: list[Issue]) -> list[Issue]:
    """按文件 + 标题 + 维度去重，故意不含行号。
    启发式切块有 20 行重叠，同一问题可能被两个 chunk 各报告一次但行号略有偏差；
    基于内容维度去重可覆盖此场景。
    """
    seen: set[str] = set()
    result = []
    for issue in issues:
        key = f"{issue.file}:{issue.title.lower()}:{issue.dimension}"
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _prepare_issues(results: list[ReviewResult]) -> list[Issue]:
    """去重 → 排序 → 浅拷贝 → 重编号，两个 render 函数共用此流程。
    浅拷贝保证两次独立渲染互不干扰（每次 renumber_issue_ids 操作副本，
    Issue 各字段均为不可变标量，浅拷贝已足够）。
    """
    all_issues: list[Issue] = [i for r in results for i in r.issues]
    all_issues = deduplicate(all_issues)
    all_issues.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 3), x.file, x.line_start))
    all_issues = [copy.copy(i) for i in all_issues]
    renumber_issue_ids(all_issues)
    return all_issues


def render_markdown(results: list[ReviewResult], scoring: ScoringConfig) -> str:
    all_issues = _prepare_issues(results)

    counts = {s: sum(1 for i in all_issues if i.severity == s)
              for s in ["critical", "high", "medium", "low"]}
    total_score = calculate_score(all_issues, scoring)

    total_input = sum(r.input_tokens for r in results)
    total_output = sum(r.output_tokens for r in results)
    reviewed_files = list(dict.fromkeys(r.file for r in results))
    # 优先使用 results 中记录的版本（审查时刻快照），回退到当前加载的版本
    report_version = next((r.prompt_version for r in results if r.prompt_version), PROMPT_VERSION)

    lines: list[str] = [
        "# Code Review Report",
        "",
        f"**审查时间**：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}  ",
        f"**审查文件数**：{len(reviewed_files)}  ",
        f"**Prompt 版本**：{report_version}  ",
        f"**Token 消耗**：输入 {total_input:,} / 输出 {total_output:,}  ",
        f"**代码质量评分**：{total_score} / 100",
        "",
        "---",
        "",
        "## 问题总览",
        "",
        "| 严重级别 | 数量 |",
        "|---------|------|",
        f"| 🔴 Critical | {counts['critical']} |",
        f"| 🟠 High     | {counts['high']} |",
        f"| 🟡 Medium   | {counts['medium']} |",
        f"| 🔵 Low      | {counts['low']} |",
        f"| **合计**    | **{len(all_issues)}** |",
        "",
        "---",
    ]

    for severity in ["critical", "high", "medium", "low"]:
        level_issues = [i for i in all_issues if i.severity == severity]
        if not level_issues:
            continue
        emoji = SEVERITY_EMOJI[severity]
        lines += ["", f"## {emoji} {severity.capitalize()} 问题", ""]
        for issue in level_issues:
            line_note = "" if issue.line_verified else " ⚠️ *行号待人工确认*"
            lines += [
                f"### [{issue.id}] {issue.title}",
                "",
                f"- **文件**：`{issue.file}` 第 {issue.line_start}–{issue.line_end} 行{line_note}",
                f"- **维度**：{issue.dimension}",
                f"- **描述**：{issue.description}",
                f"- **建议**：{issue.suggestion}",
            ]
            if issue.reference:
                lines.append(f"- **参考**：{issue.reference}")
            lines.append("")

    # 预分组：O(n)，避免后续文件列表循环中重复遍历 all_issues（O(n×m) → O(n+m)）
    issues_by_file: dict[str, list[Issue]] = defaultdict(list)
    for issue in all_issues:
        issues_by_file[issue.file].append(issue)

    lines += [
        "---",
        "",
        "## 审查文件列表",
        "",
    ]
    for f in reviewed_files:
        file_issues = issues_by_file[f]
        file_score = calculate_score(file_issues, scoring)
        lines.append(f"- `{f}` — {len(file_issues)} 个问题，评分 {file_score}/100")

    lines += ["", "---", "", f"*由 ai-codereview 生成 | Prompt {report_version}*", ""]
    return "\n".join(lines)


def render_sarif(results: list[ReviewResult]) -> str:
    """生成 SARIF 2.1.0 格式输出，供 GitHub Code Scanning / SonarQube 消费。"""
    all_issues = _prepare_issues(results)

    sarif_results = []
    for issue in all_issues:
        sarif_results.append({
            "ruleId": issue.id,
            "level": _SARIF_LEVEL.get(issue.severity, "warning"),
            "message": {"text": f"{issue.title}：{issue.description}"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": issue.file, "uriBaseId": "%SRCROOT%"},
                    "region": {
                        "startLine": max(issue.line_start, 1),
                        "endLine": max(issue.line_end, issue.line_start),
                    },
                }
            }],
            "properties": {
                "dimension": issue.dimension,
                "severity": issue.severity,
                "line_verified": issue.line_verified,
                "suggestion": issue.suggestion,
            },
        })

    # 调用时读取，而非模块导入时固化——测试无需在 import 前设置环境变量
    _tool_uri = os.getenv("CODEREVIEW_SARIF_URI", "")
    driver: dict = {
        "name": "ai-codereview",
        "version": "1.0.0",
        "rules": [],
    }
    if _tool_uri:
        driver["informationUri"] = _tool_uri

    sarif = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": driver},
            "results": sarif_results,
        }],
    }
    return json.dumps(sarif, ensure_ascii=False, indent=2)


def save_report(content: str, path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
