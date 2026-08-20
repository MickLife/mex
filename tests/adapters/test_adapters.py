"""M5 适配层：plain/claude/opencode 会话解析单测。"""

from mex.adapters import Format, read_dialogue
from mex.adapters.claude import parse_claude_jsonl
from mex.adapters.opencode import parse_opencode_jsonl
from mex.adapters.plain import parse_plain

CLAUDE_SAMPLE = """\
{"type": "user", "message": {"role": "user", "content": "你好"}}
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "你好！"}]}}
{"type": "system", "message": {"role": "system", "content": "system 消息"}}
not a json line
{"type": "user", "message": {"role": "user", "content": "继续"}}
"""

OPENCODE_SAMPLE = """\
{"role": "user", "content": "帮我写代码"}
{"role": "assistant", "content": [{"type": "text", "text": "好的，"}, {"type": "text", "text": "马上"}]}
{"sessionId": "abc", "parts": [{"role": "user", "content": "嵌套消息"}]}
{broken json
"""


class TestPlain:
    def test_parse_plain_passthrough(self):
        assert parse_plain("原始文本\n第二行") == "原始文本\n第二行"

    def test_read_dialogue_plain_passthrough(self):
        assert read_dialogue("原样文本", "plain") == "原样文本"


class TestClaude:
    def test_string_and_array_content(self):
        assert parse_claude_jsonl(CLAUDE_SAMPLE) == "用户：你好\n助手：你好！\n用户：继续"

    def test_system_and_broken_lines_skipped(self):
        result = parse_claude_jsonl(CLAUDE_SAMPLE)
        assert "system 消息" not in result
        assert "not a json line" not in result

    def test_empty_input(self):
        assert parse_claude_jsonl("") == ""

    def test_broken_only_no_raise(self):
        assert parse_claude_jsonl("{{{bad\njust text\n") == ""


class TestOpencode:
    def test_flat_and_nested_lines(self):
        assert parse_opencode_jsonl(OPENCODE_SAMPLE) == "用户：帮我写代码\n助手：好的，马上\n用户：嵌套消息"

    def test_broken_line_skipped(self):
        assert "broken" not in parse_opencode_jsonl(OPENCODE_SAMPLE)

    def test_non_turn_lines_ignored(self):
        text = '{"type": "session", "id": "x"}\n{"role": "user", "content": "有效消息"}\n'
        assert parse_opencode_jsonl(text) == "用户：有效消息"

    def test_empty_input(self):
        assert parse_opencode_jsonl("") == ""


class TestReadDialogue:
    def test_claude_from_file(self, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_text(CLAUDE_SAMPLE, encoding="utf-8")
        assert read_dialogue(str(p), "claude") == "用户：你好\n助手：你好！\n用户：继续"

    def test_opencode_from_file(self, tmp_path):
        p = tmp_path / "session.json"
        p.write_text(OPENCODE_SAMPLE, encoding="utf-8")
        assert read_dialogue(str(p), "opencode") == "用户：帮我写代码\n助手：好的，马上\n用户：嵌套消息"

    def test_format_literal_type(self):
        fmt: Format = "claude"
        assert fmt in ("plain", "claude", "opencode")
