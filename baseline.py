from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from parser import Issue

logger = logging.getLogger("codereview")

# 基线文件格式版本。升级哈希算法时递增此常量，load_baseline 会检测旧版本并给出明确提示。
# v1 = MD5（已废弃），v2 = SHA-256（当前）
_BASELINE_VERSION = 2


@functools.lru_cache(maxsize=256)
def _normalize_path(file_path: str) -> str:
    """归一化路径为 git 根目录相对路径，确保不同机器上同一文件生成相同的 hash。
    按优先级依次尝试：git 根相对路径 → 绝对路径（不用 cwd 避免 lru_cache 缓存旧 cwd）。
    结果缓存：同一路径仅解析一次，避免大批量 issue 时重复遍历目录树。
    """
    try:
        p = Path(file_path).resolve()
        # 向上查找 .git 目录，使用 git 根的相对路径（CI 与本地机器保持一致）
        for parent in [p.parent, *p.parent.parents]:
            if (parent / ".git").exists():
                return p.relative_to(parent).as_posix()
        # 回退：绝对路径（不依赖 cwd，防止测试中 os.chdir 导致缓存失效）
        return p.as_posix()
    except Exception:
        return file_path


def _issue_hash(issue: Issue) -> str:
    """生成问题唯一指纹。
    - 不含行号：代码重构后行号变化但问题不变，不应使基线失效。
    - 不含 severity：LLM 重新审查时 severity 可能升/降级，不应使基线失效产生误报。
    - 路径归一化：从不同工作目录运行时，同一文件的 hash 保持一致。
    """
    normalized = _normalize_path(issue.file)
    key = f"{normalized}|{issue.dimension}|{issue.title}"
    return hashlib.sha256(key.encode()).hexdigest()


def load_baseline(path: str) -> set[str]:
    """加载已知问题的哈希集合，文件不存在时返回空集。
    若基线文件版本低于当前版本（哈希算法已升级），返回空集并打印迁移提示。
    """
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        file_version = data.get("version", 1)
        if file_version < _BASELINE_VERSION:
            logger.warning(
                "基线文件格式版本过旧（v%d，当前需要 v%d），哈希算法已升级（MD5 → SHA-256），"
                "将视为空基线重建。请运行带 --update-baseline 的命令生成新基线：%s",
                file_version, _BASELINE_VERSION, path,
            )
            return set()
        return set(data.get("hashes", []))
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        # AttributeError：JSON 根节点为非 dict（列表等）时 .get() 抛出
        # TypeError：hashes 字段为非可迭代类型（如 null/integer）时 set() 抛出
        logger.warning("基线文件解析失败，将视为空基线：%s", e)
        return set()


def save_baseline(issues: list[Issue], path: str) -> None:
    """将当前所有问题的哈希写入基线文件（原子写，防止中途崩溃损坏文件）。"""
    # dict.fromkeys 去重同时保留顺序
    hashes = list(dict.fromkeys(_issue_hash(i) for i in issues))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"version": _BASELINE_VERSION, "hashes": hashes}, ensure_ascii=False, indent=2)
    # 写临时文件后 rename，保证原子性
    fd, tmp_path = tempfile.mkstemp(dir=p.parent, prefix=".baseline_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, p)
    except Exception:
        # suppress 防止 unlink 失败（文件已被删除/权限问题）用新异常覆盖原始异常
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    logger.info("基线已更新：%d 个问题 → %s", len(hashes), path)


def filter_new_issues(issues: list[Issue], baseline: set[str]) -> list[Issue]:
    """过滤掉基线中已知的问题，只返回新增问题。"""
    return [i for i in issues if _issue_hash(i) not in baseline]
