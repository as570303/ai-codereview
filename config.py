from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("codereview")
_T = TypeVar("_T")

load_dotenv()  # 仅模块首次导入时执行一次，避免 load_config() 每次调用都重复加载

VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low"})
# 按实际严重度排序的列表，用于错误消息（frozenset 排序是字母序，非严重度顺序）
_SEVERITY_ORDERED: list[str] = ["critical", "high", "medium", "low"]

VALID_OUTPUT_FORMATS: frozenset[str] = frozenset({"markdown", "sarif"})

# 数值型字段的期望类型：YAML 若写了带引号的数字（如 temperature: "0.5"），
# 强制转换为正确类型，否则后续比较会抛 TypeError 而非友好的 ValueError。
# 此字典是静态配置，提升为模块级常量避免每次 load_config() 调用时重新创建。
_NUMERIC_COERCE: dict[str, type] = {
    "temperature": float,
    "max_file_size_kb": int,
    "max_output_tokens": int,
}


@dataclass
class ScoringConfig:
    critical_weight: int = 20
    high_weight: int = 10
    medium_weight: int = 3
    low_weight: int = 1


@dataclass
class OutputConfig:
    formats: list[str] = field(default_factory=lambda: ["markdown"])
    report_path: str = "./code-review-report.md"


@dataclass
class ConcurrencyConfig:
    max_workers: int = 5
    rate_limit_rpm: int = 50


@dataclass
class RetryConfig:
    max_attempts: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 30.0
    timeout: float = 60.0  # 单次 API 调用超时秒数


@dataclass
class ReviewConfig:
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.0
    language: str = "auto"
    ignore_paths: list[str] = field(default_factory=list)
    severity_threshold: str = "low"
    custom_rules_path: str | None = None
    baseline_path: str = "./.codereview-baseline.json"
    desensitize: bool = True
    use_local: bool = False
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = "codellama"
    use_cli: bool = False          # 用 claude CLI 子进程替代 SDK，复用 Claude Code 订阅额度
    max_file_size_kb: int = 512    # 单文件最大读取大小，超出则跳过
    max_output_tokens: int = 8192  # 单次 LLM 调用最大输出 tokens
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)

    @property
    def api_key(self) -> str:
        """返回 Anthropic API Key。仅在 use_local=False 时由 create_client 调用。"""
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key.strip():
            raise RuntimeError(
                "ANTHROPIC_API_KEY 未设置。请复制 .env.example 为 .env 并填入 API Key。"
            )
        return key


def _apply_nested(cfg_obj: _T, raw: dict, cls: type[_T], fields: list[str]) -> _T:
    """将 raw dict 中出现的字段覆盖到 dataclass，其余字段保留 cfg_obj 现有值。
    :param fields: 要处理的字段名列表（决定读哪些 key，不含默认值）
    """
    if not raw:
        return cfg_obj
    # 检测未知配置键（常见 typo 防护），有未知键时发出警告而非静默忽略
    unknown = set(raw.keys()) - set(fields)
    if unknown:
        logger.warning("配置 [%s] 包含未知字段，将被忽略（可能是拼写错误）：%s",
                       cls.__name__, sorted(unknown))
    kwargs = {k: raw.get(k, getattr(cfg_obj, k)) for k in fields}
    return cls(**kwargs)


def load_config(config_path: str = ".codereview.yml") -> ReviewConfig:
    path = Path(config_path)
    if not path.exists():
        return ReviewConfig()

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 根节点不是 dict（如空文件、纯字符串、数字）时回退为空配置，避免 AttributeError
    if not isinstance(raw, dict):
        return ReviewConfig()

    cfg = ReviewConfig()

    # 顶层标量字段统一映射
    for key in ("model", "temperature", "language", "ignore_paths",
                "severity_threshold", "custom_rules_path", "baseline_path",
                "desensitize", "use_local", "local_base_url", "local_model",
                "use_cli", "max_file_size_kb", "max_output_tokens"):
        if key not in raw:
            continue
        val = raw[key]
        # ignore_paths 必须为列表；写成字符串会导致按字符迭代，静默产生错误匹配
        if key == "ignore_paths" and not isinstance(val, list):
            raise ValueError(
                f"ignore_paths 必须为列表格式，当前值：{val!r}（类型：{type(val).__name__}）。\n"
                f"正确格式示例：\n  ignore_paths:\n    - '*.pyc'\n    - 'node_modules/'"
            )
        # 数值型字段强制类型转换，防止 YAML 带引号数字（如 "0.5"）导致 TypeError
        if key in _NUMERIC_COERCE and not isinstance(val, _NUMERIC_COERCE[key]):
            try:
                val = _NUMERIC_COERCE[key](val)
            except (ValueError, TypeError):
                raise ValueError(
                    f"配置字段 {key!r} 类型错误：期望 {_NUMERIC_COERCE[key].__name__}，"
                    f"实际得到 {type(val).__name__}（值：{val!r}）"
                )
        setattr(cfg, key, val)

    cfg.scoring = _apply_nested(
        cfg.scoring, raw.get("scoring", {}), ScoringConfig,
        ["critical_weight", "high_weight", "medium_weight", "low_weight"],
    )
    cfg.output = _apply_nested(
        cfg.output, raw.get("output", {}), OutputConfig,
        ["formats", "report_path"],
    )
    cfg.concurrency = _apply_nested(
        cfg.concurrency, raw.get("concurrency", {}), ConcurrencyConfig,
        ["max_workers", "rate_limit_rpm"],
    )
    cfg.retry = _apply_nested(
        cfg.retry, raw.get("retry", {}), RetryConfig,
        ["max_attempts", "backoff_base", "backoff_max", "timeout"],
    )

    # 配置校验
    cfg.severity_threshold = cfg.severity_threshold.lower()  # 规范化大小写，避免 "High" 静默退化
    if cfg.severity_threshold not in VALID_SEVERITIES:
        raise ValueError(
            f"severity_threshold 无效值 {cfg.severity_threshold!r}，"
            f"有效值（按严重度排序）：{_SEVERITY_ORDERED}"
        )
    # formats 规范化（大小写）+ 校验合法值
    cfg.output.formats = [f.lower() for f in cfg.output.formats]
    invalid_formats = [f for f in cfg.output.formats if f not in VALID_OUTPUT_FORMATS]
    if invalid_formats:
        raise ValueError(
            f"output.formats 包含无效值 {invalid_formats}，"
            f"有效值：{sorted(VALID_OUTPUT_FORMATS)}"
        )
    if cfg.max_file_size_kb <= 0:
        raise ValueError(f"max_file_size_kb 必须 > 0，当前值：{cfg.max_file_size_kb}")
    if not (0.0 <= cfg.temperature <= 1.0):
        raise ValueError(
            f"temperature 必须在 [0.0, 1.0] 范围内，当前值：{cfg.temperature}"
        )
    if cfg.max_output_tokens <= 0:
        raise ValueError(f"max_output_tokens 必须 > 0，当前值：{cfg.max_output_tokens}")
    if cfg.concurrency.max_workers <= 0:
        raise ValueError(
            f"concurrency.max_workers 必须 > 0，当前值：{cfg.concurrency.max_workers}。"
            f"值为 0 会导致 asyncio.Semaphore(0) 使所有任务永久阻塞。"
        )

    return cfg
