"""核心模块单元测试。

运行：
    pytest tests/ -v
"""
from __future__ import annotations

import pytest

# ── parser ────────────────────────────────────────────────────────────────────

from parser import (
    Issue, ReviewResult,
    calculate_score, filter_by_threshold, deduplicate, renumber_issue_ids,
)
from config import ScoringConfig


def _make_issue(severity: str, title: str = "test", dimension: str = "security",
                file: str = "a.py", line_start: int = 1) -> Issue:
    return Issue(
        id="X-001", dimension=dimension, severity=severity,
        file=file, line_start=line_start, line_end=line_start,
        line_verified=True, title=title, description="", suggestion="",
    )


class TestCalculateScore:
    def test_no_issues(self):
        assert calculate_score([], ScoringConfig()) == 100

    def test_critical_deducts_20(self):
        issues = [_make_issue("critical")]
        assert calculate_score(issues, ScoringConfig()) == 80

    def test_multiple_severities(self):
        issues = [
            _make_issue("critical"),   # -20
            _make_issue("high"),       # -10
            _make_issue("medium"),     # -3
            _make_issue("low"),        # -1
        ]
        assert calculate_score(issues, ScoringConfig()) == 66

    def test_score_floor_is_zero(self):
        issues = [_make_issue("critical")] * 10
        assert calculate_score(issues, ScoringConfig()) == 0


class TestFilterByThreshold:
    def test_low_threshold_keeps_all(self):
        issues = [
            _make_issue("critical"), _make_issue("high"),
            _make_issue("medium"),   _make_issue("low"),
        ]
        assert len(filter_by_threshold(issues, "low")) == 4

    def test_high_threshold_drops_medium_low(self):
        issues = [
            _make_issue("critical"), _make_issue("high"),
            _make_issue("medium"),   _make_issue("low"),
        ]
        result = filter_by_threshold(issues, "high")
        severities = {i.severity for i in result}
        assert severities == {"critical", "high"}

    def test_critical_only(self):
        issues = [_make_issue("critical"), _make_issue("high")]
        result = filter_by_threshold(issues, "critical")
        assert len(result) == 1 and result[0].severity == "critical"

    def test_invalid_threshold_treated_as_low(self):
        # SEVERITY_ORDER.get returns 3 (low level) for unknown keys
        issues = [_make_issue("critical"), _make_issue("low")]
        result = filter_by_threshold(issues, "INVALID")
        assert len(result) == 2


class TestDeduplicate:
    def test_exact_duplicate_removed(self):
        i1 = _make_issue("high", title="SQL Injection", file="a.py", line_start=10)
        i2 = _make_issue("high", title="SQL Injection", file="a.py", line_start=10)
        assert len(deduplicate([i1, i2])) == 1

    def test_same_title_different_line_deduped(self):
        """同文件同标题同维度、不同行号 → 视为 chunk 重叠产生的重复，合并为 1 条。"""
        i1 = _make_issue("high", title="SQL Injection", file="a.py", line_start=10)
        i2 = _make_issue("high", title="SQL Injection", file="a.py", line_start=20)
        assert len(deduplicate([i1, i2])) == 1

    def test_different_title_same_line_kept(self):
        """同文件同行、不同标题 → 两个不同问题，都保留。"""
        i1 = _make_issue("high", title="SQL Injection", file="a.py", line_start=10)
        i2 = _make_issue("high", title="XSS Vulnerability", file="a.py", line_start=10)
        assert len(deduplicate([i1, i2])) == 2

    def test_different_file_kept(self):
        i1 = _make_issue("high", title="SQL Injection", file="a.py", line_start=10)
        i2 = _make_issue("high", title="SQL Injection", file="b.py", line_start=10)
        assert len(deduplicate([i1, i2])) == 2

    def test_title_case_insensitive_deduped(self):
        """LLM 对同一问题用不同大小写时应被正确去重。"""
        i1 = _make_issue("high", title="SQL Injection", file="a.py")
        i2 = _make_issue("high", title="sql injection", file="a.py")
        assert len(deduplicate([i1, i2])) == 1


class TestRenumberIssueIds:
    def test_sequential_within_dimension(self):
        issues = [
            _make_issue("high", dimension="security"),
            _make_issue("high", dimension="security"),
            _make_issue("low",  dimension="logic"),
        ]
        renumber_issue_ids(issues)
        assert issues[0].id == "SEC-001"
        assert issues[1].id == "SEC-002"
        assert issues[2].id == "LOGIC-001"

    def test_empty_list(self):
        renumber_issue_ids([])  # should not raise


# ── preprocessor ──────────────────────────────────────────────────────────────

from preprocessor import preprocess


class TestPreprocess:
    def test_api_key_redacted(self):
        code = 'api_key = "sk-1234567890abcdef1234567890abcdef"'
        result = preprocess(code, enabled=True)
        assert "sk-1234" not in result.code
        assert result.redacted_count >= 1

    def test_disabled_no_change(self):
        code = 'api_key = "sk-1234567890abcdef1234567890abcdef"'
        result = preprocess(code, enabled=False)
        assert result.code == code
        assert result.redacted_count == 0

    def test_clean_code_unchanged(self):
        code = "def add(a, b):\n    return a + b\n"
        result = preprocess(code, enabled=True)
        assert result.code == code
        assert result.redacted_count == 0

    def test_bearer_token_with_padding_redacted(self):
        """Bearer token 以 base64 padding '=' 结尾时应被完整脱敏。"""
        code = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload=='
        result = preprocess(code, enabled=True)
        assert "eyJhbGci" not in result.code
        assert result.redacted_count >= 1

    def test_safe_line_preserves_newline_on_truncation(self):
        """超长行截断后应保留行尾 \\n，防止后续行与截断标记合并。"""
        from preprocessor import _safe_line, _MAX_LINE_LEN
        long_line = "x" * (_MAX_LINE_LEN + 100) + "\n"
        result = _safe_line(long_line)
        assert result.endswith("\n"), "截断后应保留行尾换行符"
        assert "TRUNCATED" in result

    def test_truncated_lines_do_not_merge(self):
        """多行代码中超长行截断后，相邻行之间应有换行分隔。"""
        from preprocessor import preprocess, _MAX_LINE_LEN
        long_line = "x" * (_MAX_LINE_LEN + 10)
        code = long_line + "\nnext_line\n"
        result = preprocess(code, enabled=True)
        assert "next_line" in result.code
        # next_line 不应与截断标记拼在同一行
        for line in result.code.splitlines():
            assert not (line.endswith("TRUNCATED]next_line")), "相邻行不应合并"


# ── tools ─────────────────────────────────────────────────────────────────────

from tools import (
    chunk_file, chunk_diff, detect_language,
    should_ignore, _chunk_heuristic, _parse_hunk_line_start,
)


class TestDetectLanguage:
    def test_python(self):
        assert detect_language("foo.py") == "python"

    def test_typescript(self):
        assert detect_language("src/App.tsx") == "typescript"

    def test_unknown(self):
        assert detect_language("README.md") == "unknown"


class TestChunkHeuristic:
    def test_small_file_single_chunk(self):
        source = "\n".join(f"line {i}" for i in range(50))
        chunks = _chunk_heuristic(source)
        assert len(chunks) == 1

    def test_large_file_multiple_chunks(self):
        source = "\n".join(f"line {i}" for i in range(400))
        chunks = _chunk_heuristic(source)
        assert len(chunks) > 1

    def test_line_numbers_correct(self):
        source = "\n".join(f"line {i}" for i in range(10))
        chunks = _chunk_heuristic(source)
        assert chunks[0].line_start == 1

    def test_empty_source(self):
        assert _chunk_heuristic("") == []

    def test_very_large_file_capped(self):
        """超过 _MAX_HEURISTIC_CHUNKS 上限时，chunk 数不得超过该上限。"""
        from tools import _MAX_HEURISTIC_CHUNKS
        # step = 150-20 = 130；生成 >100 个 chunk 需 > 100*130 = 13000 行
        source = "\n".join(f"line {i}" for i in range(14_000))
        chunks = _chunk_heuristic(source)
        assert len(chunks) == _MAX_HEURISTIC_CHUNKS


class TestChunkFilePython:
    def test_function_becomes_chunk(self):
        source = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        chunks = chunk_file("test.py", source, "python")
        names = [c.name for c in chunks]
        assert "foo" in names
        assert "bar" in names

    def test_syntax_error_falls_back_to_heuristic(self):
        source = "def broken(\n    pass\n"
        chunks = chunk_file("test.py", source, "python")
        assert len(chunks) >= 1

    def test_module_level_excludes_preamble(self):
        source = (
            "import os\n"
            "X = 1\n"
            "\n"
            "def foo():\n"
            "    pass\n"
            "\n"
            "result = foo()\n"  # module-level executable (not preamble)
        )
        chunks = chunk_file("test.py", source, "python")
        module_chunks = [c for c in chunks if c.name == "module_level"]
        if module_chunks:
            # module_level should contain 'result = foo()' but not 'import os' or 'X = 1'
            assert "import os" not in module_chunks[0].code
            assert "result = foo()" in module_chunks[0].code


class TestParseHunkLineStart:
    def test_standard_hunk_header(self):
        hunk = "@@ -10,5 +20,8 @@ def foo():\n+    new line\n"
        assert _parse_hunk_line_start(hunk) == 20

    def test_single_line_hunk(self):
        hunk = "@@ -5 +7 @@\n+line\n"
        assert _parse_hunk_line_start(hunk) == 7

    def test_fallback_on_bad_header(self):
        assert _parse_hunk_line_start("no header here") == 1


class TestChunkDiff:
    def test_empty_diff(self):
        assert chunk_diff("") == []

    def test_single_hunk_line_start(self):
        diff = "@@ -1,3 +5,4 @@\n context\n+added\n-removed\n context\n"
        chunks = chunk_diff(diff)
        assert len(chunks) == 1
        assert chunks[0].line_start == 5
        assert chunks[0].chunk_type == "diff"

    def test_no_hunk_header_fallback(self):
        diff = "some diff text without @@ markers"
        chunks = chunk_diff(diff)
        assert len(chunks) == 1

    def test_line_end_no_off_by_one(self):
        """chunk_diff 的 line_end 应为最后一行行号，而非 line_start + 行数（off-by-one）。"""
        diff = "@@ -1,3 +10,3 @@\n line1\n line2\n line3\n"
        chunks = chunk_diff(diff)
        assert len(chunks) == 1
        # line_start=10, 3 行，最后一行应为 12，不是 13
        assert chunks[0].line_start == 10
        assert chunks[0].line_end == 12


# ── baseline ──────────────────────────────────────────────────────────────────

from baseline import _issue_hash, filter_new_issues, save_baseline, load_baseline
import tempfile, os
from pathlib import Path


class TestIssueHash:
    def test_same_issue_same_hash(self):
        i = _make_issue("high", title="SQL Injection", file="a.py")
        assert _issue_hash(i) == _issue_hash(i)

    def test_different_title_different_hash(self):
        i1 = _make_issue("high", title="SQL Injection", file="a.py")
        i2 = _make_issue("high", title="XSS",           file="a.py")
        assert _issue_hash(i1) != _issue_hash(i2)

    def test_line_number_irrelevant(self):
        i1 = _make_issue("high", title="SQL Injection", file="a.py", line_start=10)
        i2 = _make_issue("high", title="SQL Injection", file="a.py", line_start=99)
        assert _issue_hash(i1) == _issue_hash(i2)


class TestFilterNewIssues:
    def test_known_issue_filtered(self):
        issue = _make_issue("high", title="SQL Injection", file="a.py")
        baseline = {_issue_hash(issue)}
        assert filter_new_issues([issue], baseline) == []

    def test_new_issue_kept(self):
        issue = _make_issue("high", title="SQL Injection", file="a.py")
        assert filter_new_issues([issue], set()) == [issue]


class TestSaveLoadBaseline:
    def test_roundtrip(self):
        issues = [
            _make_issue("high",   title="SQL Injection", file="a.py"),
            _make_issue("medium", title="XSS",           file="b.py"),
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_baseline(issues, path)
            loaded = load_baseline(path)
            assert len(loaded) == 2
            for i in issues:
                assert _issue_hash(i) in loaded
        finally:
            os.unlink(path)

    def test_deduplicate_on_save(self):
        issue = _make_issue("high", title="SQL Injection", file="a.py")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_baseline([issue, issue], path)  # duplicate input
            loaded = load_baseline(path)
            assert len(loaded) == 1
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty_set(self):
        assert load_baseline("/nonexistent/path.json") == set()

    def test_v1_baseline_returns_empty_set(self):
        """旧版 v1（MD5）基线文件不含 version 字段，应被视为不兼容并返回空集。"""
        import json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            _json.dump({"hashes": ["abc123deadbeef"]}, f)  # 无 version 字段 = v1
            path = f.name
        try:
            loaded = load_baseline(path)
            assert loaded == set(), "v1 基线应被忽略，返回空集"
        finally:
            os.unlink(path)

    def test_save_baseline_writes_version(self):
        """保存的基线文件必须包含 version 字段。"""
        import json as _json
        issue = _make_issue("high", title="Test", file="a.py")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            save_baseline([issue], path)
            data = _json.loads(Path(path).read_text())
            assert "version" in data
            assert data["version"] >= 2
        finally:
            os.unlink(path)

    def test_malformed_json_root_list_returns_empty(self):
        """JSON 根节点为列表时 .get() 抛 AttributeError，应返回空集而非崩溃。"""
        import json as _json
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(_json.dumps([{"version": 2, "hashes": ["abc"]}]))
            path = f.name
        try:
            result = load_baseline(path)
            assert result == set()
        finally:
            os.unlink(path)

    def test_issue_hash_excludes_severity(self):
        """相同文件/维度/标题但不同 severity 的 issue 应产生相同哈希（severity 不纳入哈希）。"""
        issue_med = _make_issue("medium", title="SQL Injection", file="a.py", dimension="security")
        issue_crit = _make_issue("critical", title="SQL Injection", file="a.py", dimension="security")
        assert _issue_hash(issue_med) == _issue_hash(issue_crit)


# ── config ────────────────────────────────────────────────────────────────────

from config import load_config, ReviewConfig


class TestConfigValidation:
    def test_default_config_valid(self):
        cfg = ReviewConfig()
        assert cfg.severity_threshold == "low"

    def test_invalid_severity_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("severity_threshold: INVALID\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="severity_threshold"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_invalid_max_file_size_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("max_file_size_kb: 0\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="max_file_size_kb"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_valid_config_loaded(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("severity_threshold: high\nmax_file_size_kb: 256\n")
            path = f.name
        try:
            cfg = load_config(path)
            assert cfg.severity_threshold == "high"
            assert cfg.max_file_size_kb == 256
        finally:
            os.unlink(path)

    def test_max_output_tokens_configurable(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("max_output_tokens: 16384\n")
            path = f.name
        try:
            cfg = load_config(path)
            assert cfg.max_output_tokens == 16384
        finally:
            os.unlink(path)

    def test_default_max_output_tokens(self):
        cfg = ReviewConfig()
        assert cfg.max_output_tokens == 8192

    def test_invalid_temperature_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("temperature: 2.5\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="temperature"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_temperature_boundary_valid(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("temperature: 0.0\n")
            path = f.name
        try:
            cfg = load_config(path)
            assert cfg.temperature == 0.0
        finally:
            os.unlink(path)

    def test_invalid_max_output_tokens_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("max_output_tokens: 0\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="max_output_tokens"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_severity_threshold_case_normalized(self):
        """severity_threshold: High 应被规范化为 'high'，而非静默退化为 low 行为。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("severity_threshold: High\n")
            path = f.name
        try:
            cfg = load_config(path)
            assert cfg.severity_threshold == "high"
        finally:
            os.unlink(path)

    def test_max_workers_zero_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("concurrency:\n  max_workers: 0\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="max_workers"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_ignore_paths_string_raises(self):
        """ignore_paths 写成字符串（而非列表）应给出明确错误，而非静默按字符匹配。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("ignore_paths: '*.pyc'\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="ignore_paths"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_yaml_root_non_dict_returns_default(self):
        """YAML 根节点为非 dict（如纯字符串）时应回退为默认配置，而非 AttributeError。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("just a string\n")
            path = f.name
        try:
            cfg = load_config(path)
            assert cfg.model == ReviewConfig().model
        finally:
            os.unlink(path)

    def test_temperature_default_is_float(self):
        """temperature 默认值应为 float 0.0，而非 int 0。"""
        cfg = ReviewConfig()
        assert isinstance(cfg.temperature, float)

    def test_invalid_output_format_raises(self):
        """output.formats 中包含无效值时应给出明确错误。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("output:\n  formats:\n    - pdf\n")
            path = f.name
        try:
            with pytest.raises(ValueError, match="output.formats"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_output_format_case_normalized(self):
        """output.formats: [Markdown] 应被规范化为 'markdown'。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write("output:\n  formats:\n    - Markdown\n    - SARIF\n")
            path = f.name
        try:
            cfg = load_config(path)
            assert cfg.output.formats == ["markdown", "sarif"]
        finally:
            os.unlink(path)


# ── new feature coverage (Round 4) ────────────────────────────────────────────

from tools import is_code_file


class TestIsCodeFile:
    def test_python_supported(self):
        assert is_code_file("app.py") is True

    def test_yaml_supported(self):
        assert is_code_file("config.yml") is True
        assert is_code_file("k8s.yaml") is True

    def test_hcl_supported(self):
        assert is_code_file("main.tf") is True
        assert is_code_file("vars.hcl") is True

    def test_markdown_not_supported(self):
        assert is_code_file("README.md") is False

    def test_image_not_supported(self):
        assert is_code_file("logo.png") is False


class TestYamlHclDetection:
    def test_detect_yaml(self):
        from tools import detect_language
        assert detect_language("deploy.yml") == "yaml"
        assert detect_language("values.yaml") == "yaml"

    def test_detect_hcl(self):
        from tools import detect_language
        assert detect_language("main.tf") == "hcl"
        assert detect_language("backend.hcl") == "hcl"


class TestPreambleCap:
    """preamble 超过 30 行时应被截断，避免对每个 chunk 重复发送大量 import。"""

    def test_preamble_truncated(self):
        from tools import chunk_file
        # 生成 50 行 import 的 Python 文件
        imports = "\n".join(f"import mod_{i}" for i in range(50))
        code = imports + "\n\ndef my_func():\n    x = 1\n    return x\n"
        chunks = chunk_file("test.py", code, "python")
        func_chunk = next((c for c in chunks if c.name == "my_func"), None)
        assert func_chunk is not None, "my_func chunk not found"
        # preamble 部分行数不应超过 30 + 几行 overhead
        preamble_section = func_chunk.code.split("# [function/class")[0]
        line_count = preamble_section.count("\n")
        assert line_count <= 35, f"preamble not truncated, got {line_count} lines"
        assert "truncated" in func_chunk.code


class TestRenderIsolation:
    """render_markdown 和 render_sarif 不应互相污染 Issue.id（深拷贝保护）。"""

    def test_original_issue_id_unchanged_after_render(self):
        from parser import render_markdown, render_sarif, ReviewResult
        from config import ScoringConfig

        original_id = "ORIGINAL-ID"
        issue = _make_issue("high", title="Isolation Test")
        issue.id = original_id
        result = ReviewResult(file="a.py", language="python", issues=[issue])

        # 依次调用两个 render 函数
        render_markdown([result], ScoringConfig())
        render_sarif([result])

        # 原始 Issue 对象 id 不应被修改
        assert issue.id == original_id

    def test_markdown_and_sarif_have_consistent_ids(self):
        from parser import render_markdown, render_sarif, ReviewResult
        import json as _json
        from config import ScoringConfig

        issue = _make_issue("critical", title="Consistent ID")
        result = ReviewResult(file="a.py", language="python", issues=[issue])

        md = render_markdown([result], ScoringConfig())
        sarif_str = render_sarif([result])
        sarif = _json.loads(sarif_str)

        # 从 markdown 中提取 ID（格式：### [SEC-001] ...）
        import re
        md_ids = re.findall(r"\[([A-Z]+-\d+)\]", md)
        sarif_ids = [r["ruleId"] for r in sarif["runs"][0]["results"]]

        assert set(md_ids) == set(sarif_ids)

    def test_render_markdown_uses_result_prompt_version(self):
        """render_markdown 应使用 ReviewResult 中记录的 prompt_version，而非硬编码当前版本。"""
        from parser import render_markdown, ReviewResult
        from config import ScoringConfig

        result = ReviewResult(file="a.py", language="python", issues=[])
        result.prompt_version = "v9.9"
        md = render_markdown([result], ScoringConfig())
        assert "v9.9" in md


# ── tools: max_line_from_diff & should_ignore ─────────────────────────────────

from tools import max_line_from_diff, should_ignore


class TestMaxLineFromDiff:
    def test_single_hunk(self):
        diff = "@@ -1,5 +10,8 @@\n context\n+added\n"
        # start=10, count=8 → max = 10 + 8 - 1 = 17
        assert max_line_from_diff(diff) == 17

    def test_multiple_hunks_returns_max(self):
        diff = "@@ -1,3 +5,4 @@\n line\n@@ -20,2 +100,10 @@\n line\n"
        # hunk1: 5+4-1=8, hunk2: 100+10-1=109
        assert max_line_from_diff(diff) == 109

    def test_no_count_defaults_to_one(self):
        # @@ -5 +7 @@ (no comma/count)
        diff = "@@ -5 +7 @@\n+line\n"
        assert max_line_from_diff(diff) == 7

    def test_empty_diff_returns_one(self):
        assert max_line_from_diff("") == 1

    def test_no_hunk_headers_returns_one(self):
        assert max_line_from_diff("plain text no headers") == 1


class TestListCodeFiles:
    """list_code_files 的 os.walk + 目录剪枝实现测试。"""

    def _make_tree(self, tmp_path, structure: dict):
        """递归创建文件树，structure 格式：{name: content_str | nested_dict}。"""
        for name, val in structure.items():
            p = tmp_path / name
            if isinstance(val, dict):
                p.mkdir(parents=True, exist_ok=True)
                self._make_tree(p, val)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(val or "")

    def test_basic_discovery(self, tmp_path):
        self._make_tree(tmp_path, {"a.py": "", "b.js": "", "README.md": ""})
        from tools import list_code_files
        files = list_code_files(str(tmp_path), [])
        basenames = [Path(f).name for f in files]
        assert "a.py" in basenames
        assert "b.js" in basenames
        assert "README.md" not in basenames  # 非代码文件

    def test_result_is_sorted(self, tmp_path):
        self._make_tree(tmp_path, {"z.py": "", "src": {"a.py": ""}, "m.py": ""})
        from tools import list_code_files
        files = list_code_files(str(tmp_path), [])
        assert files == sorted(files), "list_code_files 应返回全局字母排序结果"

    def test_git_dir_pruned(self, tmp_path):
        self._make_tree(tmp_path, {".git": {"HEAD": "ref: refs/heads/main", "objects": {"pack": {}}},
                                    "app.py": ""})
        from tools import list_code_files
        files = list_code_files(str(tmp_path), [])
        assert all(".git" not in f for f in files), ".git 目录内容不应被纳入审查"

    def test_ignore_pattern_prunes_directory(self, tmp_path):
        self._make_tree(tmp_path, {"node_modules": {"lodash": {"index.js": ""}}, "app.py": ""})
        from tools import list_code_files
        files = list_code_files(str(tmp_path), ["node_modules"])
        names = [Path(f).name for f in files]
        assert "index.js" not in names
        assert "app.py" in names

    def test_ignore_pattern_matches_file(self, tmp_path):
        self._make_tree(tmp_path, {"app.py": "", "generated.py": ""})
        from tools import list_code_files
        files = list_code_files(str(tmp_path), ["generated.py"])
        names = [Path(f).name for f in files]
        assert "generated.py" not in names
        assert "app.py" in names

    def test_empty_ignore_returns_all(self, tmp_path):
        self._make_tree(tmp_path, {"a.py": "", "b.ts": ""})
        from tools import list_code_files
        files = list_code_files(str(tmp_path), [])
        assert len(files) == 2

    def test_empty_directory(self, tmp_path):
        from tools import list_code_files
        assert list_code_files(str(tmp_path), []) == []

    def test_symlink_file_skipped(self, tmp_path):
        real = tmp_path / "real.py"
        real.write_text("")
        link = tmp_path / "link.py"
        link.symlink_to(real)
        from tools import list_code_files
        files = list_code_files(str(tmp_path), [])
        basenames = [Path(f).name for f in files]
        assert "real.py" in basenames
        assert "link.py" not in basenames  # 符号链接文件应跳过


class TestShouldIgnore:
    def test_exact_filename_match(self):
        assert should_ignore("src/config.py", ["config.py"]) is True

    def test_glob_pattern_match(self):
        assert should_ignore("src/generated/foo.py", ["generated/*"]) is True

    def test_directory_pattern(self):
        assert should_ignore("node_modules/lib/index.js", ["node_modules"]) is True

    def test_no_match(self):
        assert should_ignore("src/app.py", ["vendor", "*.min.js"]) is False

    def test_empty_patterns(self):
        assert should_ignore("anything.py", []) is False

    def test_wildcard_extension(self):
        assert should_ignore("dist/bundle.min.js", ["*.min.js"]) is True
