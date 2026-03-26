from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections.abc import Callable, Coroutine
from typing import Any

import anthropic

from config import RetryConfig, ReviewConfig
from parser import Issue, ReviewResult, parse_llm_response, calculate_score
from prompts import build_system_prompt, build_user_prompt
from tools import CodeChunk

logger = logging.getLogger("codereview")

# chunk 级全局并发上限：review.py 和 eval.py 共用此常量，防止各自硬编码 magic number
_MAX_CHUNK_CONCURRENCY = 10

# Structured Output 工具定义（强制 LLM 返回 JSON Schema）
REVIEW_TOOL: dict[str, Any] = {
    "name": "submit_review",
    "description": "提交代码审查结果",
    "input_schema": {
        "type": "object",
        "required": ["issues"],
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "dimension", "severity", "line_start", "line_end",
                                 "title", "description", "suggestion"],
                    "properties": {
                        "id":          {"type": "string", "description": "问题编号，如 SEC-001"},
                        "dimension":   {"type": "string", "enum": ["security", "logic", "performance", "quality"]},
                        "severity":    {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "line_start":  {"type": "integer", "description": "问题起始行号"},
                        "line_end":    {"type": "integer", "description": "问题结束行号"},
                        "title":       {"type": "string", "description": "问题简短标题"},
                        "description": {"type": "string", "description": "问题详细描述"},
                        "suggestion":  {"type": "string", "description": "具体修复建议（含示例代码）"},
                        "reference":   {"type": "string", "description": "参考标准，如 OWASP A03:2021"},
                    },
                },
            },
        },
    },
}

# 模型输入/输出单价（USD / 1M tokens），可按需调整
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6":  (3.0, 15.0),
    "claude-opus-4-6":    (15.0, 75.0),
    "claude-haiku-4-5-20251001": (0.8, 4.0),
}
_DEFAULT_PRICING = (3.0, 15.0)


def _get_pricing(model: str) -> tuple[float, float]:
    """获取模型定价（USD/1M tokens）。
    优先读取环境变量覆盖，格式：CODEREVIEW_PRICING_<MODEL>=input:output
    例：CODEREVIEW_PRICING_CLAUDE_SONNET_4_6=3.0:15.0
    支持前缀匹配：用户配置 claude-haiku-4-5 可匹配定价键 claude-haiku-4-5-20251001，
    反之亦然，防止日期后缀不一致导致费用估算退化为 Sonnet 默认定价。
    """
    env_key = "CODEREVIEW_PRICING_" + model.upper().replace("-", "_").replace(".", "_")
    env_val = os.environ.get(env_key, "")
    if env_val:
        try:
            inp_s, out_s = env_val.split(":")
            return float(inp_s), float(out_s)
        except (ValueError, TypeError):
            logger.warning("环境变量格式错误，使用内置定价：%s=%s（期望格式：input:output）", env_key, env_val)
    # 精确匹配
    if model in _PRICING:
        return _PRICING[model]
    # 前缀匹配：定价键与配置 model 互为前缀（以 "-" 分隔避免误匹配）
    for key, pricing in _PRICING.items():
        if key.startswith(model + "-") or model.startswith(key + "-"):
            return pricing
    return _DEFAULT_PRICING


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """按输入/输出分别计算费用，返回 USD。"""
    in_price, out_price = _get_pricing(model)
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


class RateLimiter:
    """令牌桶限流器，控制每分钟最大请求数。

    - lock 只保护时间戳读写，sleep 在 lock 外执行，避免持锁 sleep 串行化。
    - burst=5：空闲后允许最多 5 个请求立即通过，避免冷启动时不必要的等待。
    """

    def __init__(self, rpm: int, burst: int = 5) -> None:
        self._interval = 60.0 / max(rpm, 1)
        # 空闲积分上限：最多积累 burst 个令牌（防止长时间空闲后的突发涌入）
        self._burst_credits = self._interval * burst
        self._lock = asyncio.Lock()
        self._next_allowed: float = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # 将 _next_allowed 限制在 now - burst_credits 以上，防止无限积分
            self._next_allowed = max(self._next_allowed, now - self._burst_credits)
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed += self._interval
        # sleep 在 lock 外，其他协程可以同时进入 lock 预约各自的时间槽
        if wait > 0:
            await asyncio.sleep(wait)


async def _call_with_retry(
    fn: Callable[[], Coroutine[Any, Any, Any]],
    retry_cfg: RetryConfig,
) -> Any:
    """重试封装：指数退避。attempts = max(max_attempts, 1) >= 1，保证循环至少执行一次，
    last_exc 在 raise 前一定已被赋值。
    """
    attempts = max(retry_cfg.max_attempts, 1)
    last_exc: Exception = RuntimeError("未知错误")  # 保证 raise 前一定已赋值（满足 type checker）
    for attempt in range(attempts):
        try:
            return await fn()
        except (anthropic.APITimeoutError,
                anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            # APIStatusError 只重试过载/服务端错误（429/500/502/503/504/529）
            # 注：RateLimitError 是 APIStatusError 的子类（status=429），已被上面的子句覆盖
            if isinstance(e, anthropic.APIStatusError) and e.status_code not in (
                429, 500, 502, 503, 504, 529
            ):
                raise
            last_exc = e
            if attempt == attempts - 1:
                break
            wait = min(retry_cfg.backoff_base ** (attempt + 1) + random.random(), retry_cfg.backoff_max)
            logger.warning("API 调用失败（第 %d 次），%.1fs 后重试：%s", attempt + 1, wait, e)
            await asyncio.sleep(wait)
    raise last_exc


def create_client(cfg: ReviewConfig) -> anthropic.AsyncAnthropic:
    """创建 AsyncAnthropic 客户端，应在调用方统一创建并复用。"""
    if cfg.use_local:
        return anthropic.AsyncAnthropic(
            api_key="ollama",
            base_url=cfg.local_base_url,
        )
    return anthropic.AsyncAnthropic(api_key=cfg.api_key)


# ── claude CLI 后端 ──────────────────────────────────────────────────────────

# 从 CLI 响应文本中提取 JSON 对象的正则（贪婪匹配最外层 {}）
_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)
_JSON_OBJ_RE   = re.compile(r'\{.*"issues".*\}', re.DOTALL)

# CLI 模式下追加到 user prompt 末尾，要求 LLM 返回 JSON
_CLI_JSON_INSTRUCTION = """

---
请严格按以下 JSON 格式输出审查结果，不要在 JSON 之外输出任何文字：
{"issues": [{"id": "SEC-001", "dimension": "security", "severity": "critical", "line_start": 1, "line_end": 1, "title": "标题", "description": "描述", "suggestion": "建议"}]}
dimension 必须是：security / logic / performance / quality
severity  必须是：critical / high / medium / low
如果没有发现问题，直接输出：{"issues": []}"""


def _extract_json_from_cli(text: str) -> dict[str, Any]:
    """从 claude CLI 纯文本响应中提取 JSON 审查结果。
    依次尝试：直接解析 → markdown 代码块 → 正则查找 issues 对象 → 返回空结果。
    """
    text = text.strip()
    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2. markdown ```json ... ``` 代码块
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. 正则定位 {"issues": ...}
    m = _JSON_OBJ_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("CLI 响应无法解析为 JSON，返回空结果。响应片段：%.200s", text)
    return {"issues": []}


async def _call_claude_cli(system: str, user_msg: str, timeout: float = 120.0) -> str:
    """通过 claude CLI 子进程调用 LLM，复用 Claude Code 订阅，无需 ANTHROPIC_API_KEY。"""
    full_prompt = f"{system}\n\n{user_msg}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", full_prompt, "--output-format", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"claude CLI 调用超时（>{timeout}s）")
    except FileNotFoundError:
        raise RuntimeError("未找到 claude 命令，请先安装 Claude Code CLI：https://claude.ai/code")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"claude CLI 返回错误（code={proc.returncode}）：{err}")
    return stdout.decode(errors="replace")


async def review_chunks(
    file_path: str,
    language: str,
    chunks: list[CodeChunk],
    total_lines: int,
    cfg: ReviewConfig,
    custom_rules: str = "",
    client: anthropic.AsyncAnthropic | None = None,
    rate_limiter: RateLimiter | None = None,
    chunk_semaphore: asyncio.Semaphore | None = None,
) -> ReviewResult:
    """审查一个文件的所有 chunk，chunks 内部并发执行。"""
    # 若调用方未传入 client，本函数创建并负责关闭（避免 httpx 连接池泄漏）
    # CLI 模式无需 SDK client，跳过创建以避免触发 ANTHROPIC_API_KEY 校验
    _own_client = client is None and not cfg.use_cli
    if _own_client:
        client = create_client(cfg)

    system = build_system_prompt(language, custom_rules)
    start = time.monotonic()

    async def _review_one_chunk(chunk: CodeChunk) -> tuple[list[Issue], int, int]:
        user_msg = build_user_prompt(file_path, chunk.name, language, chunk.code,
                                     chunk_type=chunk.chunk_type, line_start=chunk.line_start)

        if cfg.use_cli:
            # ── claude CLI 模式：通过子进程调用，复用 Claude Code 订阅额度 ──
            user_msg_with_json = user_msg + _CLI_JSON_INSTRUCTION

            async def _call_cli():
                return await _call_claude_cli(system, user_msg_with_json,
                                              timeout=cfg.retry.timeout)

            raw_text = await _call_with_retry(_call_cli, cfg.retry)
            raw_dict = _extract_json_from_cli(raw_text)
            issues = parse_llm_response(raw_dict, file_path, total_lines)
            return issues, 0, 0  # CLI 模式无 token 计数

        # ── SDK 模式（默认）────────────────────────────────────────────────────
        async def _call():
            if rate_limiter:
                await rate_limiter.acquire()
            return await client.messages.create(
                model=cfg.local_model if cfg.use_local else cfg.model,
                max_tokens=cfg.max_output_tokens,
                temperature=cfg.temperature,
                system=system,
                tools=[REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "submit_review"},
                messages=[{"role": "user", "content": user_msg}],
                timeout=cfg.retry.timeout,  # 可通过 .codereview.yml retry.timeout 配置
            )

        response = await _call_with_retry(_call, cfg.retry)
        inp = response.usage.input_tokens
        out = response.usage.output_tokens
        issues = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_review":
                issues = parse_llm_response(block.input, file_path, total_lines)
                break
        return issues, inp, out

    # chunk 级并发上限：优先使用调用方传入的全局 Semaphore（跨文件共享，防止总并发失控）；
    # 未传入时按本文件 chunk 数创建局部限流器（单文件上限 10）。
    _chunk_sem = chunk_semaphore or asyncio.Semaphore(max(min(len(chunks), _MAX_CHUNK_CONCURRENCY), 1))

    async def _bounded_chunk(chunk: CodeChunk) -> tuple[list[Issue], int, int]:
        async with _chunk_sem:
            return await _review_one_chunk(chunk)

    # 同一文件的 chunks 并发审查；return_exceptions=True 保证单 chunk 失败不影响其他 chunk
    try:
        chunk_results = await asyncio.gather(
            *[_bounded_chunk(c) for c in chunks],
            return_exceptions=True,
        )
    finally:
        # finally 保证无论 gather 内部是否有未预期异常，owned client 都会被关闭
        if _own_client:
            await client.aclose()

    all_issues: list[Issue] = []
    total_input = total_output = 0
    for i, res in enumerate(chunk_results):
        if isinstance(res, BaseException):
            logger.error("chunk %s 审查失败，跳过：%s", chunks[i].name, res)
            continue
        issues, inp, out = res
        all_issues.extend(issues)
        total_input += inp
        total_output += out

    duration = time.monotonic() - start
    result = ReviewResult(
        file=file_path,
        language=language,
        issues=all_issues,
        # score 不在此处计算：issues 后续会经过 filter/deduplicate，
        # 最终评分统一由 render_markdown / _print_summary 在过滤后计算，避免二次计算不一致。
        model=cfg.local_model if cfg.use_local else cfg.model,
        input_tokens=total_input,
        output_tokens=total_output,
    )

    score = calculate_score(all_issues, cfg.scoring)
    if cfg.use_cli:
        logger.info(
            "审查完成 file=%s lang=%s issues=%d score=%d cost=N/A(cli) duration=%.1fs",
            file_path, language, len(all_issues), score, duration,
        )
    elif cfg.use_local:
        logger.info(
            "审查完成 file=%s lang=%s issues=%d score=%d tokens=%d+%d cost=N/A(local) duration=%.1fs",
            file_path, language, len(all_issues), score,
            total_input, total_output, duration,
        )
    else:
        cost = estimate_cost(cfg.model, total_input, total_output)
        logger.info(
            "审查完成 file=%s lang=%s issues=%d score=%d tokens=%d+%d cost=$%.4f duration=%.1fs",
            file_path, language, len(all_issues), score,
            total_input, total_output, cost, duration,
        )
    return result
