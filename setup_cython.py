"""
Cython 构建配置 — 将所有核心模块编译为 .so 二进制，源码不可见。
用法：
  python setup_cython.py build_ext --inplace   # 编译 .so 到当前目录
  python setup_cython.py bdist_wheel           # 生成平台专属 .whl（不含 .py 源码）
"""
from __future__ import annotations

from setuptools import Extension, setup
from Cython.Build import cythonize

# 需要编译的核心模块（review.py 保留为纯 Python，Typer CLI 依赖 inspect 反射）
_MODULES = [
    "baseline",
    "config",
    "eval",
    "llm_client",
    "parser",
    "preprocessor",
    "prompts",
    "tools",
]

ext_modules = cythonize(
    [Extension(name=m, sources=[f"{m}.py"]) for m in _MODULES],
    compiler_directives={
        "language_level": "3",   # 使用 Python 3 语义
        "boundscheck": False,    # 关闭边界检查，提升性能
        "wraparound": False,     # 关闭负索引检查
    },
)

setup(
    name="ai-codereview",
    version="1.0.0",
    description="基于 Claude 的多维度 AI 代码审查工具",
    python_requires=">=3.11",
    install_requires=[
        "anthropic>=0.40.0",
        "python-dotenv>=1.0.0",
        "pyyaml>=6.0",
        "typer>=0.12.0",
        "rich>=13.0.0",
    ],
    extras_require={
        "git": ["gitpython>=3.1.0"],
    },
    entry_points={
        "console_scripts": ["codereview=review:app"],
    },
    ext_modules=ext_modules,
    # 注意：不声明 py_modules，wheel 中只含 .so，不含 .py 源码
)
