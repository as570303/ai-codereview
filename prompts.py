from __future__ import annotations

import functools
import os

from tools import CHUNK_TYPE_DIFF

# 支持通过环境变量 CODEREVIEW_PROMPT_VERSION 覆盖，方便 CI 注入版本号
PROMPT_VERSION: str = os.environ.get("CODEREVIEW_PROMPT_VERSION", "v1.0")

# 语言专项审查规则（覆盖 _EXT_MAP 中的全部 14 种语言）
_LANGUAGE_RULES: dict[str, str] = {
    "python": (
        "Python 专项：PEP8 命名规范、类型注解缺失、资源未用 with 语句、"
        "可变默认参数（def f(x=[])）、裸 except、GIL 相关并发问题。"
    ),
    "javascript": (
        "JavaScript 专项：XSS（innerHTML/eval）、原型链污染、== 代替 ===、"
        "未处理的 Promise rejection、var 代替 let/const、回调地狱。"
    ),
    "typescript": (
        "TypeScript 专项：any 类型滥用、类型断言强制转换、非空断言 !、"
        "未处理的 Promise rejection、接口与类型别名滥用。"
    ),
    "java": (
        "Java 专项：空指针未检查、资源未关闭（try-with-resources）、"
        "线程安全问题、泛型擦除、字符串用 == 比较。"
    ),
    "go": (
        "Go 专项：错误处理忽略（_ 丢弃 error）、goroutine 泄漏、"
        "channel 死锁、接口设计过度、defer 在循环中的陷阱。"
    ),
    "sql": (
        "SQL 专项：SQL 注入、缺少索引、SELECT *、N+1 查询模式、"
        "事务边界不清晰、未处理 NULL 值。"
    ),
    "ruby": (
        "Ruby 专项：符号注入、不安全的 eval/send、质量问题（过长方法、缺少冻结字符串注释）、"
        "ActiveRecord 中的 N+1 查询、未处理异常（rescue Exception）。"
    ),
    "php": (
        "PHP 专项：SQL 注入（拼接查询）、XSS（未转义输出）、文件包含漏洞、"
        "弱类型比较（==）、未验证的用户输入直接使用。"
    ),
    "csharp": (
        "C# 专项：空引用异常（未使用 null 条件运算符）、资源未释放（IDisposable）、"
        "async/await 死锁（.Result/.Wait()）、LINQ 延迟执行陷阱、不安全的反序列化。"
    ),
    "cpp": (
        "C++ 专项：缓冲区溢出、use-after-free、double free、未初始化变量、"
        "整数溢出、裸指针（应用 smart pointer）、资源泄漏。"
    ),
    "c": (
        "C 专项：缓冲区溢出（strcpy/sprintf）、整数溢出/截断、use-after-free、"
        "NULL 指针解引用、格式字符串漏洞、资源泄漏（malloc 未 free）。"
    ),
    "rust": (
        "Rust 专项：unsafe 块使用是否必要、unwrap()/expect() 在生产代码中滥用、"
        "Mutex 中 panic 导致中毒（poisoned）、clone() 性能浪费、错误处理用 ? 而非 unwrap。"
    ),
    "swift": (
        "Swift 专项：强制解包（!）滥用、循环引用（weak/unowned 缺失）、"
        "主线程 UI 更新未在 DispatchQueue.main 中执行、内存泄漏（闭包捕获 self）。"
    ),
    "kotlin": (
        "Kotlin 专项：!! 非空断言滥用、协程泄漏（未取消 Job）、"
        "伴生对象中的内存泄漏、Java 互操作时的平台类型空安全问题。"
    ),
    "shell": (
        "Shell 专项：未加引号的变量（word splitting 风险）、命令注入、"
        "未检查命令退出码（set -e / || exit）、使用 $() 代替反引号、路径含空格未处理。"
    ),
    "yaml": (
        "YAML/IaC 专项：Kubernetes YAML 中容器以 root 运行、缺少资源限制（limits/requests）、"
        "镜像使用 latest tag、Secret 明文写入、hostNetwork/hostPID 特权配置、"
        "RBAC 权限过宽（wildcards）。"
    ),
    "hcl": (
        "Terraform/HCL 专项：S3/GCS bucket 公开访问、安全组开放 0.0.0.0/0、"
        "IAM 策略 Action:* Resource:*、未加密的存储卷、硬编码凭证、"
        "未启用 MFA Delete、state 文件存储未加密。"
    ),
}

BASE_SYSTEM_PROMPT = """\
你是一位拥有 10 年经验的资深代码审查专家，精通安全、性能、代码质量和逻辑正确性。

## 审查维度（按优先级）
1. **安全（Security）**：SQL 注入、XSS、硬编码密码/Token、权限绕过、敏感信息泄露
2. **逻辑（Logic）**：空指针/空引用、边界条件、死循环、竞态条件、异常未捕获
3. **性能（Performance）**：N+1 查询、不必要的循环嵌套、内存泄漏、重复计算
4. **质量（Quality）**：命名不规范、函数过长（>50行）、重复代码、魔法数字、缺少注释

## 严格规则
- 只报告代码中**真实存在**的问题，不要推测或过度报告
- 每个问题的 suggestion 必须包含**具体的修改示例代码**
- 行号必须精确对应代码块中的实际行
- 如果代码没有问题，返回空的 issues 数组即可

## 输出
严格按照工具定义的 JSON Schema 返回，不输出任何多余文字。
"""


@functools.lru_cache(maxsize=16)
def build_system_prompt(language: str, custom_rules: str = "") -> str:
    lang_rule = _LANGUAGE_RULES.get(language, "")
    parts = [BASE_SYSTEM_PROMPT]
    if lang_rule:
        parts.append(f"\n## {language.capitalize()} 专项规则\n{lang_rule}")
    if custom_rules:
        parts.append(f"\n## 公司/团队编码规范\n{custom_rules}")
    return "\n\n".join(parts)


def build_user_prompt(file_path: str, chunk_name: str, language: str, code: str,
                      chunk_type: str = "", line_start: int = 0) -> str:
    """构造用户 Prompt。
    :param chunk_type: CodeChunk.chunk_type，使用字段值而非名称字符串来区分 diff chunk。
    :param line_start: 代码块在原始文件中的起始行号，用于帮助 LLM 填写准确行号。
    """
    if chunk_type == CHUNK_TYPE_DIFF:
        return (
            f"请审查以下 {language} 文件的 Git diff（统一差异格式）。\n"
            f"文件路径：{file_path}\n"
            f"审查重点：'+' 开头的新增/变更行是本次改动，'-' 行是被删除的旧代码仅供上下文参考。\n"
            f"行号请基于新版本文件中的实际行号填写。\n\n"
            f"```diff\n{code}\n```"
        )
    line_hint = f"（文件实际起始行：第 {line_start} 行）" if line_start > 0 else ""
    return (
        f"请审查以下 {language} 代码。\n"
        f"文件路径：{file_path}\n"
        f"代码块：{chunk_name}{line_hint}\n\n"
        f"```{language}\n{code}\n```"
    )
