#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from pathlib import Path

import anthropic
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from baseline import load_baseline, save_baseline, filter_new_issues
from config import load_config, ReviewConfig
from llm_client import review_chunks, create_client, estimate_cost, RateLimiter, _MAX_CHUNK_CONCURRENCY
from parser import (
    ReviewResult, render_markdown, render_sarif, save_report,
    filter_by_threshold, deduplicate, calculate_score,
    SEVERITY_EMOJI,
)
from preprocessor import preprocess
from tools import (
    detect_language, read_file, chunk_file, list_code_files,
    chunk_diff, get_file_diff, is_code_file, should_ignore, max_line_from_diff,
)

# basicConfig 仅在 root logger 尚无 handler 时生效（避免在测试等场景中覆盖已有配置）
if not logging.root.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
logger = logging.getLogger("codereview")

app = typer.Typer(help="AI Code Review — 基于 Claude 的多维度代码审查工具\n\n退出码：0 = 成功，1 = 错误/文件不存在，2 = 发现 Critical 问题")
console = Console()


def _load_custom_rules(cfg: ReviewConfig) -> str:
    if not cfg.custom_rules_path:
        return ""
    path = Path(cfg.custom_rules_path)
    if not path.exists():
        logger.warning("custom_rules_path 不存在：%s", path)
        return ""
    return path.read_text(encoding="utf-8")


async def _review_diff_file(
    file_path: str,
    diff_text: str,
    cfg: ReviewConfig,
    custom_rules: str,
    client: anthropic.AsyncAnthropic | None = None,
    rate_limiter: RateLimiter | None = None,
    chunk_semaphore: asyncio.Semaphore | None = None,
) -> ReviewResult:
    """审查 diff 内容而非全文件，只关注变更行 + context。"""
    # 优先用原文件实际行数做行号校验，失败时从 diff hunk 头推断（比硬编码 9999 更准确）
    try:
        source = read_file(file_path, max_size_bytes=cfg.max_file_size_kb * 1024)
        total_lines = len(source.splitlines())
    except (ValueError, FileNotFoundError):
        total_lines = max_line_from_diff(diff_text)

    prep = preprocess(diff_text, enabled=cfg.desensitize)
    if prep.redacted_count > 0:
        console.print(f"  [yellow]脱敏[/yellow] {file_path} diff：处理 {prep.redacted_count} 处敏感信息")

    language = detect_language(file_path) if cfg.language == "auto" else cfg.language
    chunks = chunk_diff(prep.code)

    return await review_chunks(
        file_path=file_path,
        language=language,
        chunks=chunks,
        total_lines=total_lines,
        cfg=cfg,
        custom_rules=custom_rules,
        client=client,
        rate_limiter=rate_limiter,
        chunk_semaphore=chunk_semaphore,
    )


async def _review_single_file(
    file_path: str,
    cfg: ReviewConfig,
    custom_rules: str,
    client: anthropic.AsyncAnthropic | None = None,
    rate_limiter: RateLimiter | None = None,
    chunk_semaphore: asyncio.Semaphore | None = None,
) -> ReviewResult:
    source = read_file(file_path, max_size_bytes=cfg.max_file_size_kb * 1024)
    total_lines = len(source.splitlines())

    prep = preprocess(source, enabled=cfg.desensitize)
    if prep.redacted_count > 0:
        console.print(f"  [yellow]脱敏[/yellow] {file_path}：处理 {prep.redacted_count} 处敏感信息")

    language = detect_language(file_path) if cfg.language == "auto" else cfg.language
    chunks = chunk_file(file_path, prep.code, language)

    return await review_chunks(
        file_path=file_path,
        language=language,
        chunks=chunks,
        total_lines=total_lines,
        cfg=cfg,
        custom_rules=custom_rules,
        client=client,
        rate_limiter=rate_limiter,
        chunk_semaphore=chunk_semaphore,
    )


def _print_summary(results: list[ReviewResult], cfg: ReviewConfig) -> None:
    # issues 在调用方已经过 filter_by_threshold，这里不再重复过滤（避免双重过滤导致评分不一致）
    all_issues = deduplicate([i for r in results for i in r.issues])

    counts = {s: sum(1 for i in all_issues if i.severity == s)
              for s in ["critical", "high", "medium", "low"]}
    score = calculate_score(all_issues, cfg.scoring)

    table = Table(title="Code Review 汇总", show_header=True, header_style="bold cyan")
    table.add_column("严重级别", style="bold")
    table.add_column("数量", justify="right")
    table.add_row(f"{SEVERITY_EMOJI['critical']} Critical", str(counts["critical"]))
    table.add_row(f"{SEVERITY_EMOJI['high']} High",         str(counts["high"]))
    table.add_row(f"{SEVERITY_EMOJI['medium']} Medium",     str(counts["medium"]))
    table.add_row(f"{SEVERITY_EMOJI['low']} Low",           str(counts["low"]))
    table.add_row("合计", str(len(all_issues)))
    console.print(table)

    color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
    console.print(f"\n代码质量评分：[bold {color}]{score} / 100[/bold {color}]")

    total_input  = sum(r.input_tokens  for r in results)
    total_output = sum(r.output_tokens for r in results)
    if cfg.use_cli:
        console.print("Token 消耗：N/A（claude CLI 模式）")
    elif cfg.use_local:
        console.print(f"Token 消耗：输入 {total_input:,} / 输出 {total_output:,}（本地模型，无费用）")
    else:
        cost = estimate_cost(cfg.model, total_input, total_output)
        console.print(
            f"Token 消耗：输入 {total_input:,} / 输出 {total_output:,}（约 ${cost:.4f} USD）"
        )


def _save_outputs(results: list[ReviewResult], cfg: ReviewConfig, report_path: str) -> None:
    """按配置的 formats 分别保存报告。formats 已在 load_config 中规范化为小写。"""
    formats = cfg.output.formats
    stem = Path(report_path).with_suffix("")

    if "markdown" in formats:
        md_path = report_path if report_path.endswith(".md") else str(stem) + ".md"
        save_report(render_markdown(results, cfg.scoring), md_path)
        console.print(f"[green]Markdown 报告：[/green]{md_path}")

    if "sarif" in formats:
        sarif_path = str(stem) + ".sarif"
        save_report(render_sarif(results), sarif_path)
        console.print(f"[green]SARIF 报告：[/green]{sarif_path}")


def _exit_on_critical(results: list[ReviewResult]) -> None:
    if any(i.severity == "critical" for r in results for i in r.issues):
        console.print("[red bold]存在 Critical 问题，建议在合并前修复！[/red bold]")
        raise typer.Exit(2)


def _finalize_results(
    results: list[ReviewResult],
    cfg: ReviewConfig,
    output: str | None,
    incremental: bool,
    update_baseline: bool,
) -> None:
    """三个 CLI 命令共用的收尾逻辑：增量过滤 → 汇总打印 → 保存报告 → 更新基线 → critical 退出。"""
    if incremental:
        baseline = load_baseline(cfg.baseline_path)
        for r in results:
            r.issues = filter_new_issues(r.issues, baseline)
        total_new = sum(len(r.issues) for r in results)
        console.print(f"[dim]增量模式：过滤已知问题后剩余 {total_new} 个新增问题[/dim]")

    _print_summary(results, cfg)
    _save_outputs(results, cfg, output or cfg.output.report_path)

    if update_baseline:
        save_baseline([i for r in results for i in r.issues], cfg.baseline_path)

    _exit_on_critical(results)


# ── 公共选项 ────────────────────────────────────────────────────────────────

_OPT_CONFIG          = typer.Option(".codereview.yml", "--config", "-c", help="配置文件路径")
_OPT_OUTPUT          = typer.Option(None, "--output", "-o", help="报告输出路径")
_OPT_LOCAL           = typer.Option(False, "--local", help="使用本地 Ollama 模型（不调用 Claude API）")
_OPT_USE_CLI         = typer.Option(False, "--use-cli", help="通过 claude CLI 子进程调用，复用 Claude Code 订阅额度")
_OPT_NODESENSITIZE   = typer.Option(False, "--no-desensitize", help="关闭脱敏（仅测试用）")
_OPT_INCREMENTAL     = typer.Option(False, "--incremental", help="只报告相对于基线的新增问题")
_OPT_UPDATE_BASELINE = typer.Option(False, "--update-baseline", help="审查完成后更新基线文件")


# ── CLI 命令 ──────────────────────────────────────────────────────────────────

@app.command()
def file(
    file_path: str = typer.Argument(..., help="要审查的代码文件路径"),
    config_file: str = _OPT_CONFIG,
    output: str | None = _OPT_OUTPUT,
    local: bool = _OPT_LOCAL,
    use_cli: bool = _OPT_USE_CLI,
    no_desensitize: bool = _OPT_NODESENSITIZE,
    incremental: bool = _OPT_INCREMENTAL,
    update_baseline: bool = _OPT_UPDATE_BASELINE,
):
    """审查单个代码文件。"""
    cfg = load_config(config_file)
    if local:
        cfg.use_local = True
    if use_cli:
        cfg.use_cli = True
    if no_desensitize:
        cfg.desensitize = False

    if not Path(file_path).exists():
        console.print(f"[red]文件不存在：{file_path}[/red]")
        raise typer.Exit(1)

    if cfg.use_cli:
        mode = "Claude CLI"
    elif cfg.use_local:
        mode = "[yellow]本地模型[/yellow]"
    else:
        mode = "Claude API"
    console.print(f"\n[bold cyan]开始审查：[/bold cyan]{file_path}（{mode}）")

    custom_rules = _load_custom_rules(cfg)

    async def _run_file():
        # 在 event loop 内创建 asyncio 原语，与 directory/diff 命令保持一致
        rate_limiter = RateLimiter(cfg.concurrency.rate_limit_rpm)
        chunk_semaphore = asyncio.Semaphore(_MAX_CHUNK_CONCURRENCY)
        # CLI 模式无需 SDK client；async with nullcontext(None) 等价于跳过
        client_ctx = nullcontext(None) if cfg.use_cli else create_client(cfg)
        async with client_ctx as client:
            return await _review_single_file(
                file_path, cfg, custom_rules,
                client=client, rate_limiter=rate_limiter, chunk_semaphore=chunk_semaphore,
            )

    try:
        result = asyncio.run(_run_file())
    except Exception as e:
        # 捕获所有异常（文件过大 ValueError、API 错误等），给用户友好提示
        console.print(f"[red]审查失败：{e}[/red]")
        raise typer.Exit(1)
    result.issues = filter_by_threshold(result.issues, cfg.severity_threshold)

    _finalize_results([result], cfg, output, incremental, update_baseline)


@app.command()
def directory(
    dir_path: str = typer.Argument(".", help="要审查的目录路径"),
    config_file: str = _OPT_CONFIG,
    output: str | None = _OPT_OUTPUT,
    local: bool = _OPT_LOCAL,
    use_cli: bool = _OPT_USE_CLI,
    no_desensitize: bool = _OPT_NODESENSITIZE,
    max_files: int = typer.Option(50, "--max-files", help="最多审查文件数"),
    incremental: bool = _OPT_INCREMENTAL,
    update_baseline: bool = _OPT_UPDATE_BASELINE,
):
    """审查整个目录（并发）。"""
    cfg = load_config(config_file)
    if local:
        cfg.use_local = True
    if use_cli:
        cfg.use_cli = True
    if no_desensitize:
        cfg.desensitize = False

    files = list_code_files(dir_path, cfg.ignore_paths)[:max_files]
    if not files:
        console.print("[yellow]未找到可审查的代码文件[/yellow]")
        raise typer.Exit(0)

    if cfg.use_cli:
        mode = "Claude CLI"
    elif cfg.use_local:
        mode = "[yellow]本地模型[/yellow]"
    else:
        mode = "Claude API"
    console.print(f"\n[bold cyan]目录审查：[/bold cyan]{dir_path}（{len(files)} 个文件，{mode}）")

    custom_rules = _load_custom_rules(cfg)

    async def _run_all():
        # CLI 模式无需 SDK client；async with nullcontext(None) 等价于跳过
        client_ctx = nullcontext(None) if cfg.use_cli else create_client(cfg)
        async with client_ctx as client:
            rate_limiter = RateLimiter(cfg.concurrency.rate_limit_rpm)
            semaphore = asyncio.Semaphore(cfg.concurrency.max_workers)
            chunk_semaphore = asyncio.Semaphore(_MAX_CHUNK_CONCURRENCY)

            async def _bounded(fp):
                async with semaphore:
                    try:
                        return await _review_single_file(
                            fp, cfg, custom_rules,
                            client=client, rate_limiter=rate_limiter,
                            chunk_semaphore=chunk_semaphore,
                        )
                    except Exception as e:
                        logger.error("文件审查失败，跳过：%s — %s", fp, e)
                        return ReviewResult(file=fp, language=detect_language(fp))

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("审查中...", total=len(files))
                results = []
                for coro in asyncio.as_completed([_bounded(f) for f in files]):
                    r = await coro
                    r.issues = filter_by_threshold(r.issues, cfg.severity_threshold)
                    results.append(r)
                    progress.advance(task)
            return results

    results = asyncio.run(_run_all())
    _finalize_results(results, cfg, output, incremental, update_baseline)


@app.command()
def diff(
    base: str = typer.Argument("HEAD~1", help="对比的基准 commit/branch"),
    config_file: str = _OPT_CONFIG,
    output: str | None = _OPT_OUTPUT,
    local: bool = _OPT_LOCAL,
    use_cli: bool = _OPT_USE_CLI,
    no_desensitize: bool = _OPT_NODESENSITIZE,
    incremental: bool = _OPT_INCREMENTAL,
    update_baseline: bool = _OPT_UPDATE_BASELINE,
    context_lines: int = typer.Option(20, "--context", help="diff 前后保留的上下文行数"),
):
    """审查 Git diff（只审查变更的代码）。"""
    try:
        import git as gitpython
    except ImportError:
        console.print(
            "[red]缺少依赖 gitpython，请运行：[/red]\n"
            "  [bold]pip install gitpython[/bold]\n"
            "或在 pyproject.toml 中加入 extras_require: git"
        )
        raise typer.Exit(1)

    cfg = load_config(config_file)
    if local:
        cfg.use_local = True
    if use_cli:
        cfg.use_cli = True
    if no_desensitize:
        cfg.desensitize = False

    try:
        repo = gitpython.Repo(search_parent_directories=True)
    except gitpython.InvalidGitRepositoryError:
        console.print("[red]当前目录不是 Git 仓库[/red]")
        raise typer.Exit(1)

    # UNCOMMITTED 是特殊关键字，审查工作区（含暂存）相对 HEAD 的未提交变更
    _UNCOMMITTED = {"UNCOMMITTED", "uncommitted"}
    try:
        if base in _UNCOMMITTED:
            changed = repo.git.diff("--name-only", "HEAD").splitlines()
            # 同时包含 untracked 文件中的代码文件
            untracked = [f for f in repo.untracked_files if is_code_file(f)]
            changed = list(dict.fromkeys(changed + untracked))  # 去重保序
        else:
            changed = repo.git.diff("--name-only", base, "HEAD").splitlines()
    except gitpython.GitCommandError as e:
        console.print(f"[red]git diff 失败：{e}[/red]")
        raise typer.Exit(1)

    # 只保留支持审查的代码文件（排除 ignore_paths）；已删除文件记录 debug 日志
    changed_code = []
    for f in changed:
        if not is_code_file(f):
            continue
        if not (Path(repo.working_dir) / f).exists():
            logger.debug("跳过已删除文件（不在工作目录中）：%s", f)
            continue
        if cfg.ignore_paths and should_ignore(f, cfg.ignore_paths):
            continue
        changed_code.append(f)

    if not changed_code:
        console.print("[yellow]diff 范围内没有可审查的代码文件变更[/yellow]")
        raise typer.Exit(0)

    if cfg.use_cli:
        mode = "Claude CLI"
    elif cfg.use_local:
        mode = "[yellow]本地模型[/yellow]"
    else:
        mode = "Claude API"
    console.print(
        f"\n[bold cyan]Diff 审查：[/bold cyan]{base}..HEAD — {len(changed_code)} 个变更文件（{mode}）"
    )
    custom_rules = _load_custom_rules(cfg)

    async def _run_diff():
        # CLI 模式无需 SDK client；async with nullcontext(None) 等价于跳过
        client_ctx = nullcontext(None) if cfg.use_cli else create_client(cfg)
        async with client_ctx as client:
            rate_limiter = RateLimiter(cfg.concurrency.rate_limit_rpm)
            semaphore = asyncio.Semaphore(cfg.concurrency.max_workers)
            chunk_semaphore = asyncio.Semaphore(_MAX_CHUNK_CONCURRENCY)

            async def _bounded_diff(fp):
                async with semaphore:
                    try:
                        diff_text = get_file_diff(repo, base, fp, context_lines)
                        if not diff_text.strip():
                            return await _review_single_file(
                                fp, cfg, custom_rules,
                                client=client, rate_limiter=rate_limiter,
                                chunk_semaphore=chunk_semaphore,
                            )
                        return await _review_diff_file(
                            fp, diff_text, cfg, custom_rules,
                            client=client, rate_limiter=rate_limiter,
                            chunk_semaphore=chunk_semaphore,
                        )
                    except Exception as e:
                        logger.error("文件审查失败，跳过：%s — %s", fp, e)
                        return ReviewResult(file=fp, language=detect_language(fp))

            with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                          TaskProgressColumn(), console=console) as progress:
                task = progress.add_task("审查变更文件...", total=len(changed_code))
                results = []
                for coro in asyncio.as_completed([_bounded_diff(f) for f in changed_code]):
                    r = await coro
                    r.issues = filter_by_threshold(r.issues, cfg.severity_threshold)
                    results.append(r)
                    progress.advance(task)
            return results

    results = asyncio.run(_run_diff())
    _finalize_results(results, cfg, output, incremental, update_baseline)


if __name__ == "__main__":
    app()
