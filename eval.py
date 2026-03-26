#!/usr/bin/env python3
"""
eval.py — 黄金测试集评估脚本

用法：
  python eval.py --dataset ./golden_dataset
  python eval.py --dataset ./golden_dataset --prompt-version v1.1

评估指标：
  Precision = 真正发现的问题 / AI 报告的总问题数  （衡量误报率）
  Recall    = AI 发现的已知问题 / 已知总问题数    （衡量漏报率）
  F1        = 2 × Precision × Recall / (P + R)
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import anthropic
import typer
from rich.console import Console
from rich.table import Table

from config import load_config, ReviewConfig
from llm_client import create_client, review_chunks, RateLimiter, _MAX_CHUNK_CONCURRENCY
from parser import filter_by_threshold
from preprocessor import preprocess
from prompts import PROMPT_VERSION
from tools import detect_language, read_file, chunk_file

logger = logging.getLogger("eval")
console = Console()
app = typer.Typer(help="黄金测试集评估 — 衡量 AI 审查的 Precision / Recall / F1")

# 维度前缀 → dimension 映射（模块级常量，避免在每次调用中重建）
DIM_MAP: dict[str, str] = {
    "SEC": "security", "LOGIC": "logic", "PERF": "performance", "QUAL": "quality",
}

# 黄金测试集期望结果定义（默认内置，可通过 --expectations 覆盖）
# 格式：{ 文件相对路径: [ 期望发现的问题 id 前缀 列表 ] }
GOLDEN_EXPECTATIONS: dict[str, list[str]] = {
    "security/sql_injection.py":    ["SEC"],
    "security/hardcoded_secret.py": ["SEC"],
    "security/xss_vulnerability.js": ["SEC"],
    "logic/null_pointer.py":        ["LOGIC"],
    "logic/race_condition.go":      ["LOGIC"],
    "performance/n_plus_one.py":    ["PERF"],
    "clean/no_issues.py":           [],          # 期望 0 问题（测试误报）
}


async def _eval_file(
    file_path: Path,
    expected_prefixes: list[str],
    cfg: ReviewConfig,
    client: anthropic.AsyncAnthropic,
    rate_limiter: RateLimiter,
    semaphore: asyncio.Semaphore,
    chunk_semaphore: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    async with semaphore:
        source = read_file(str(file_path), max_size_bytes=cfg.max_file_size_kb * 1024)
        total_lines = len(source.splitlines())
        prep = preprocess(source, enabled=cfg.desensitize)
        language = detect_language(str(file_path))
        chunks = chunk_file(str(file_path), prep.code, language)

        result = await review_chunks(
            file_path=str(file_path),
            language=language,
            chunks=chunks,
            total_lines=total_lines,
            cfg=cfg,
            client=client,
            rate_limiter=rate_limiter,
            chunk_semaphore=chunk_semaphore,
        )

        found_issues = filter_by_threshold(result.issues, cfg.severity_threshold)

        expected_count = len(expected_prefixes)
        total_found = len(found_issues)

        # 判断期望前缀中有多少被覆盖：对每个期望前缀（含重复），检查是否找到对应维度的问题。
        # 用 sum 而非 set，避免多个相同前缀（如 ["SEC","SEC"]）因集合去重导致 Recall 偏低。
        found_dims = {i.dimension for i in found_issues}
        hit = sum(1 for p in expected_prefixes if DIM_MAP.get(p) in found_dims)

        expected_dims = {DIM_MAP[p] for p in expected_prefixes if p in DIM_MAP}
        # clean 文件：期望 0 问题，任何报告都算误报
        if expected_count == 0:
            true_positives = 0
            false_positives = total_found
        else:
            true_positives = hit
            # FP = 在"非期望维度"上报告的问题（而非粗糙的 total - hit）
            false_positives = sum(1 for i in found_issues if i.dimension not in expected_dims)

        return {
            "file": file_path.name,
            "expected": expected_count,
            "found": total_found,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "issues": [{"id": i.id, "dim": i.dimension, "sev": i.severity, "title": i.title}
                       for i in found_issues],
        }


@app.command()
def run(
    dataset: str = typer.Option("./golden_dataset", "--dataset", "-d", help="黄金测试集目录"),
    config_file: str = typer.Option(".codereview.yml", "--config", "-c"),
    prompt_version: str | None = typer.Option(None, "--prompt-version", help="仅做记录，不影响运行"),
    output: str | None = typer.Option(None, "--output", "-o", help="结果 JSON 输出路径"),
    expectations: str | None = typer.Option(
        None, "--expectations", "-e",
        help="自定义期望结果 JSON 文件（格式同 GOLDEN_EXPECTATIONS），不指定则使用内置默认值",
    ),
):
    """对黄金测试集运行审查并计算 Precision / Recall / F1。"""
    cfg = load_config(config_file)
    dataset_path = Path(dataset)

    if not dataset_path.exists():
        console.print(f"[red]测试集目录不存在：{dataset_path}[/red]")
        raise typer.Exit(1)

    if expectations:
        exp_path = Path(expectations)
        if not exp_path.exists():
            console.print(f"[red]期望文件不存在：{exp_path}[/red]")
            raise typer.Exit(1)
        golden = json.loads(exp_path.read_text(encoding="utf-8"))
    else:
        golden = GOLDEN_EXPECTATIONS

    version = prompt_version or PROMPT_VERSION
    console.print(f"\n[bold cyan]黄金测试集评估[/bold cyan]  Prompt 版本：{version}")

    async def _run_all():
        # async with 保证 httpx 连接池在 event loop 内正确关闭，消除 ResourceWarning
        async with create_client(cfg) as client:
            rate_limiter = RateLimiter(cfg.concurrency.rate_limit_rpm)
            semaphore = asyncio.Semaphore(cfg.concurrency.max_workers)
            chunk_semaphore = asyncio.Semaphore(_MAX_CHUNK_CONCURRENCY)
            tasks = []
            keys = []
            for rel_path, expected in golden.items():
                fp = dataset_path / rel_path
                if not fp.exists():
                    logger.warning("测试文件不存在，跳过：%s", fp)
                    continue
                tasks.append(_eval_file(fp, expected, cfg, client, rate_limiter, semaphore, chunk_semaphore))
                keys.append(rel_path)

            if not tasks:
                return []

            raw = await asyncio.gather(*tasks, return_exceptions=True)
            results = []
            for key, res in zip(keys, raw):
                if isinstance(res, Exception):
                    logger.error("评估文件失败 %s：%s", key, res)
                    results.append({"file": Path(key).name, "expected": len(golden[key]),
                                     "found": 0, "true_positives": 0, "false_positives": 0, "issues": []})
                elif isinstance(res, BaseException):
                    # CancelledError / KeyboardInterrupt 等不应被吞掉，直接重新抛出
                    raise res
                else:
                    results.append(res)
            return results

    file_results = asyncio.run(_run_all())

    # 汇总统计
    total_tp = sum(r["true_positives"]  for r in file_results)
    total_fp = sum(r["false_positives"] for r in file_results)
    total_expected = sum(r["expected"]  for r in file_results)

    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 1.0
    recall    = total_tp / total_expected         if total_expected > 0           else 1.0
    f1        = (2 * precision * recall / (precision + recall)
                 if (precision + recall) > 0 else 0.0)

    # 打印文件级结果
    table = Table(title="评估结果（文件级）", show_header=True, header_style="bold cyan")
    table.add_column("文件")
    table.add_column("期望", justify="right")
    table.add_column("发现", justify="right")
    table.add_column("TP", justify="right")
    table.add_column("FP", justify="right")
    for r in file_results:
        tp_color = "green" if r["true_positives"] == r["expected"] else "red"
        table.add_row(
            r["file"],
            str(r["expected"]),
            str(r["found"]),
            f"[{tp_color}]{r['true_positives']}[/{tp_color}]",
            str(r["false_positives"]),
        )
    console.print(table)

    # 打印汇总指标
    p_color = "green" if precision >= 0.75 else "red"
    r_color = "green" if recall    >= 0.80 else "red"
    f_color = "green" if f1        >= 0.77 else "red"

    console.print(f"\n[bold]Precision:[/bold] [{p_color}]{precision:.2%}[/{p_color}]  "
                  f"（目标 ≥ 75%）")
    console.print(f"[bold]Recall:   [/bold] [{r_color}]{recall:.2%}[/{r_color}]  "
                  f"（目标 ≥ 80%）")
    console.print(f"[bold]F1 Score: [/bold] [{f_color}]{f1:.2%}[/{f_color}]")

    # 保存 JSON 结果
    eval_result = {
        "prompt_version": version,
        "precision": round(precision, 4),
        "recall":    round(recall,    4),
        "f1":        round(f1,        4),
        "files":     file_results,
    }
    if output:
        Path(output).write_text(json.dumps(eval_result, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"\n[green]评估结果已保存：[/green]{output}")

    # 低于目标时非零退出，方便 CI 检测 Prompt 质量回退
    if precision < 0.75 or recall < 0.80:
        console.print("\n[red bold]评估未达标，请检查 Prompt 是否回退！[/red bold]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
