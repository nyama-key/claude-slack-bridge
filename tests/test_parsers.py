"""パーサー/JSONL処理関数のテスト。

対象: _classify_jsonl_entry, _format_ask_user_question, _format_exit_plan_mode,
      _read_new_jsonl_entries, parse_task_id, _detect_permission_prompt,
      _extract_session_info_from_jsonl
"""

import json
import os

import bridge

# bridge.TaskStatus を直接参照する（from bridge import すると別オブジェクトになる可能性がある）
TaskStatus = bridge.TaskStatus


# ── _classify_jsonl_entry ─────────────────────────────────

class TestClassifyJsonlEntry:
    def test_thinking(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "abc" * 100}],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 1
        cat, text, meta = results[0]
        assert cat == "status"
        assert "thinking" in text
        assert "300" in text  # 300文字
        assert "```" in text  # コードブロックで全文表示
        assert "abc" * 100 in text  # thinking全文が含まれる
        assert meta is None

    def test_text(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello world"}],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 1
        cat, text, meta = results[0]
        assert cat == "text"
        assert text == "Hello world"
        assert meta is None

    def test_text_empty_skipped(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "   "}],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert results == []

    def test_tool_use(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/a.py"}},
                ],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 1
        cat, text, meta = results[0]
        assert cat == "status"
        assert "`Read`" in text
        assert "/tmp/a.py" in text

    def test_tool_use_no_summary(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "SomeTool", "input": {}},
                ],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 1
        cat, text, meta = results[0]
        assert cat == "status"
        assert "`SomeTool`" in text

    def test_ask_user_question(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [{
                            "question": "Which?",
                            "options": [
                                {"label": "A"},
                                {"label": "B"},
                            ],
                        }],
                    },
                }],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 1
        cat, text, meta = results[0]
        assert cat == "question"
        assert "Which?" in text
        assert meta is not None

    def test_exit_plan_mode(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "name": "ExitPlanMode",
                    "input": {"plan": "Do something"},
                }],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 1
        cat, text, meta = results[0]
        assert cat == "question"
        assert "プランの承認" in text

    def test_empty_message(self):
        results = bridge._classify_jsonl_entry({"type": "assistant", "message": {}})
        assert results == []

    def test_non_dict_message(self):
        results = bridge._classify_jsonl_entry({"type": "assistant", "message": "string"})
        assert results == []

    def test_non_list_content(self):
        results = bridge._classify_jsonl_entry({
            "type": "assistant",
            "message": {"content": "not a list"},
        })
        assert results == []

    def test_multiple_content_items(self):
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "result"},
                ],
            },
        }
        results = bridge._classify_jsonl_entry(entry)
        assert len(results) == 2
        assert results[0][0] == "status"  # thinking
        assert results[1][0] == "text"    # text


# ── _format_ask_user_question ─────────────────────────────

class TestFormatAskUserQuestion:
    def test_single_question(self):
        tool_input = {
            "questions": [{
                "question": "Choose one",
                "options": [
                    {"label": "Option A", "description": "First option"},
                    {"label": "Option B"},
                ],
            }],
        }
        text, meta = bridge._format_ask_user_question(tool_input)
        assert "Choose one" in text
        assert "Option A" in text
        assert "First option" in text
        assert "Option B" in text
        assert meta is not None
        assert len(meta["questions"]) == 1

    def test_multi_question(self):
        tool_input = {
            "questions": [
                {
                    "question": "Q1",
                    "options": [{"label": "A"}, {"label": "B"}],
                },
                {
                    "question": "Q2",
                    "options": [{"label": "C"}, {"label": "D"}],
                },
            ],
        }
        text, meta = bridge._format_ask_user_question(tool_input)
        assert "Q1" in text
        assert "Q2" in text
        assert len(meta["questions"]) == 2

    def test_multi_select(self):
        tool_input = {
            "questions": [{
                "question": "Pick",
                "options": [{"label": "X"}, {"label": "Y"}],
                "multiSelect": True,
            }],
        }
        text, meta = bridge._format_ask_user_question(tool_input)
        assert "複数選択" in text
        assert meta["questions"][0]["multi_select"] is True

    def test_empty_questions(self):
        text, meta = bridge._format_ask_user_question({"questions": []})
        assert text == ""
        assert meta is None

    def test_no_questions_key(self):
        text, meta = bridge._format_ask_user_question({})
        assert text == ""
        assert meta is None

    def test_non_list_questions(self):
        text, meta = bridge._format_ask_user_question({"questions": "not a list"})
        assert text == ""
        assert meta is None

    def test_question_numbering(self):
        tool_input = {
            "questions": [{
                "question": "Pick",
                "options": [
                    {"label": "First"},
                    {"label": "Second"},
                    {"label": "Third"},
                ],
            }],
        }
        text, _ = bridge._format_ask_user_question(tool_input)
        assert "1. First" in text
        assert "2. Second" in text
        assert "3. Third" in text


# ── _format_exit_plan_mode ────────────────────────────────

class TestFormatExitPlanMode:
    def test_with_plan(self):
        text, meta = bridge._format_exit_plan_mode({"plan": "Build a feature"})
        assert "プランの承認" in text
        # プラン内容はmetadata["plan_content"]に格納（テキストにはインライン表示しない）
        assert meta["plan_content"] == "Build a feature"
        assert meta is not None
        assert len(meta["questions"]) == 1
        assert len(meta["questions"][0]["options"]) == 2

    def test_content_field(self):
        _, meta = bridge._format_exit_plan_mode({"content": "My plan"})
        assert meta["plan_content"] == "My plan"

    def test_summary_field(self):
        _, meta = bridge._format_exit_plan_mode({"summary": "Plan summary"})
        assert meta["plan_content"] == "Plan summary"

    def test_without_plan(self):
        text, meta = bridge._format_exit_plan_mode({})
        assert "プランの承認" in text
        assert meta is not None
        assert meta["plan_content"] == ""

    def test_allowed_prompts(self):
        tool_input = {
            "plan": "Do things",
            "allowedPrompts": [
                {"tool": "Bash", "prompt": "Run tests"},
            ],
        }
        _, meta = bridge._format_exit_plan_mode(tool_input)
        assert "Bash" in meta["plan_content"]
        assert "Run tests" in meta["plan_content"]
        assert "許可プロンプト" in meta["plan_content"]

    def test_long_plan_not_truncated(self):
        """プラン内容はファイルアップロードされるため、切り詰めない"""
        long_plan = "x" * 3000
        _, meta = bridge._format_exit_plan_mode({"plan": long_plan})
        assert meta["plan_content"] == long_plan

    def test_options_in_metadata(self):
        _, meta = bridge._format_exit_plan_mode({"plan": "test"})
        opts = meta["questions"][0]["options"]
        assert opts[0]["label"] == "承認して実行"
        assert opts[1]["label"] == "却下・フィードバック"


# ── _read_new_jsonl_entries ───────────────────────────────

class TestReadNewJsonlEntries:
    def test_reads_entries(self, tmp_path):
        p = tmp_path / "output.jsonl"
        lines = [
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps({"type": "result", "result": "done"}),
        ]
        p.write_text("\n".join(lines) + "\n")
        entries, offset = bridge._read_new_jsonl_entries(str(p), 0)
        assert len(entries) == 2
        assert entries[0]["type"] == "assistant"
        assert entries[1]["type"] == "result"
        assert offset > 0

    def test_reads_from_offset(self, tmp_path):
        p = tmp_path / "output.jsonl"
        line1 = json.dumps({"type": "first"}) + "\n"
        line2 = json.dumps({"type": "second"}) + "\n"
        p.write_text(line1 + line2)
        offset = len(line1.encode("utf-8"))
        entries, new_offset = bridge._read_new_jsonl_entries(str(p), offset)
        assert len(entries) == 1
        assert entries[0]["type"] == "second"

    def test_nonexistent_file(self):
        entries, offset = bridge._read_new_jsonl_entries("/nonexistent/file.jsonl", 0)
        assert entries == []
        assert offset == 0

    def test_no_new_data(self, tmp_path):
        p = tmp_path / "output.jsonl"
        content = json.dumps({"type": "test"}) + "\n"
        p.write_text(content)
        size = os.path.getsize(str(p))
        entries, offset = bridge._read_new_jsonl_entries(str(p), size)
        assert entries == []

    def test_skips_invalid_json(self, tmp_path):
        p = tmp_path / "output.jsonl"
        p.write_text('{"valid": true}\nnot json\n{"also_valid": true}\n')
        entries, _ = bridge._read_new_jsonl_entries(str(p), 0)
        assert len(entries) == 2

    def test_skips_empty_lines(self, tmp_path):
        p = tmp_path / "output.jsonl"
        p.write_text('\n\n{"type": "test"}\n\n')
        entries, _ = bridge._read_new_jsonl_entries(str(p), 0)
        assert len(entries) == 1


# ── parse_task_id ─────────────────────────────────────────

class TestParseTaskId:
    def test_hash_prefix(self):
        assert bridge.parse_task_id("#1") == 1

    def test_numeric(self):
        assert bridge.parse_task_id("1") == 1

    def test_large_number(self):
        assert bridge.parse_task_id("#123") == 123

    def test_alpha(self):
        assert bridge.parse_task_id("abc") is None

    def test_hash_only(self):
        assert bridge.parse_task_id("#") is None

    def test_empty(self):
        assert bridge.parse_task_id("") is None

    def test_whitespace(self):
        assert bridge.parse_task_id("  #5  ") == 5


# ── _detect_permission_prompt ─────────────────────────────

class TestDetectPermissionPrompt:
    def test_numbered_options(self):
        text = "Allow this tool?\n1. Yes, allow\n2. No, deny"
        result = bridge._detect_permission_prompt(text)
        assert result is not None
        assert len(result["options"]) == 2
        assert result["options"][0]["label"] == "Yes, allow"
        assert result["options"][1]["label"] == "No, deny"
        assert "Allow this tool?" in result["description"]

    def test_three_options(self):
        text = "Choose:\n1. First\n2. Second\n3. Third"
        result = bridge._detect_permission_prompt(text)
        assert result is not None
        assert len(result["options"]) == 3

    def test_multi_line_option(self):
        text = "Question?\n1. Option one\n   with continuation\n2. Option two"
        result = bridge._detect_permission_prompt(text)
        assert result is not None
        assert len(result["options"]) == 2
        assert "continuation" in result["options"][0]["label"]

    def test_less_than_two_options(self):
        text = "Just text\n1. Only one option"
        result = bridge._detect_permission_prompt(text)
        assert result is None

    def test_no_options(self):
        result = bridge._detect_permission_prompt("no numbered list here")
        assert result is None

    def test_empty(self):
        result = bridge._detect_permission_prompt("")
        assert result is None

    def test_default_description(self):
        text = "1. Yes\n2. No"
        result = bridge._detect_permission_prompt(text)
        assert result is not None
        assert result["description"] == "入力が必要です"

    def test_prompt_prefix_breaks_options(self):
        text = "1. Yes\n❯ prompt\n2. No"
        result = bridge._detect_permission_prompt(text)
        # ❯ は選択肢を終了させるので、1つだけで None になる
        assert result is None


# ── _extract_session_info_from_jsonl ──────────────────────

class TestExtractSessionInfoFromJsonl:
    def test_extracts_session_id(self, tmp_path, make_task, make_session):
        p = tmp_path / "output.jsonl"
        p.write_text(json.dumps({
            "type": "assistant",
            "message": {"sessionId": "sid-abc-123", "content": []},
        }) + "\n")
        task = make_task()
        session = make_session()
        bridge._extract_session_info_from_jsonl(task, session, str(p))
        assert session.claude_session_id == "sid-abc-123"

    def test_extracts_result(self, tmp_path, make_task):
        p = tmp_path / "output.jsonl"
        p.write_text(json.dumps({
            "type": "result",
            "result": {
                "content": [{"type": "text", "text": "Final answer"}],
            },
        }) + "\n")
        task = make_task()
        bridge._extract_session_info_from_jsonl(task, None, str(p))
        assert task.result == "Final answer"

    def test_extracts_text_from_assistant(self, tmp_path, make_task):
        p = tmp_path / "output.jsonl"
        p.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Some response"}],
            },
        }) + "\n")
        task = make_task()
        bridge._extract_session_info_from_jsonl(task, None, str(p))
        assert task.result == "Some response"

    def test_last_text_wins(self, tmp_path, make_task):
        p = tmp_path / "output.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "first"}]},
            }),
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "second"}]},
            }),
        ]
        p.write_text("\n".join(lines) + "\n")
        task = make_task()
        bridge._extract_session_info_from_jsonl(task, None, str(p))
        assert task.result == "second"

    def test_records_tool_calls(self, tmp_path, make_task):
        p = tmp_path / "output.jsonl"
        p.write_text(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }],
            },
        }) + "\n")
        task = make_task()
        bridge._extract_session_info_from_jsonl(task, None, str(p))
        assert len(task.tool_calls) == 1
        assert task.tool_calls[0]["name"] == "Bash"

    def test_nonexistent_file(self, make_task):
        task = make_task()
        # Should not raise
        bridge._extract_session_info_from_jsonl(task, None, "/nonexistent/file.jsonl")
        assert task.result is None

    def test_session_id_from_entry_level(self, tmp_path, make_task, make_session):
        """session_id が entry レベルにある場合"""
        p = tmp_path / "output.jsonl"
        p.write_text(json.dumps({
            "type": "assistant",
            "sessionId": "sid-from-entry",
            "message": {"content": []},
        }) + "\n")
        task = make_task()
        session = make_session()
        bridge._extract_session_info_from_jsonl(task, session, str(p))
        assert session.claude_session_id == "sid-from-entry"

    def test_result_as_string(self, tmp_path, make_task):
        """result が文字列の場合"""
        p = tmp_path / "output.jsonl"
        p.write_text(json.dumps({
            "type": "result",
            "result": "plain text result",
        }) + "\n")
        task = make_task()
        bridge._extract_session_info_from_jsonl(task, None, str(p))
        assert task.result == "plain text result"


# ── build_command: disallowed_tools ──────────────────────

class TestBuildCommandDisallowedTools:
    """build_command が disallowed_tools を正しく反映するかテスト"""

    def _make_runner(self):
        """テスト用の ClaudeCodeRunner（Slack依存部分をモック）"""
        from unittest.mock import MagicMock
        runner = bridge.ClaudeCodeRunner.__new__(bridge.ClaudeCodeRunner)
        runner.client = MagicMock()
        return runner

    def test_default_disallowed_tools(self, make_task):
        """デフォルト（None）ではAskUserQuestionとExitPlanMode両方が無効"""
        runner = self._make_runner()
        task = make_task(disallowed_tools=None)
        cmd = runner.build_command(task)
        idx = cmd.index("--disallowedTools")
        assert cmd[idx + 1] == "AskUserQuestion,ExitPlanMode," + bridge._BRIDGE_DIR_DENY_RULES

    def test_explicit_disallowed_tools(self, make_task):
        """明示指定ではその値が使われる（プラン承認後のケース）"""
        runner = self._make_runner()
        task = make_task(disallowed_tools="AskUserQuestion")
        cmd = runner.build_command(task)
        idx = cmd.index("--disallowedTools")
        assert cmd[idx + 1] == "AskUserQuestion," + bridge._BRIDGE_DIR_DENY_RULES

    def test_empty_disallowed_tools(self, make_task):
        """空文字列の場合もブリッジディレクトリの deny ルールだけは付与される"""
        runner = self._make_runner()
        task = make_task(disallowed_tools="")
        cmd = runner.build_command(task)
        idx = cmd.index("--disallowedTools")
        assert cmd[idx + 1] == bridge._BRIDGE_DIR_DENY_RULES


# ── _format_exit_plan_mode: is_plan_approval マーカー ────

class TestPlanApprovalMarker:
    """_format_exit_plan_mode の metadata に is_plan_approval が伝播するかテスト"""

    def test_plan_approval_marker_not_in_format(self):
        """_format_exit_plan_mode 自体は is_plan_approval を設定しない
        （_post_plan_approval が設定する）"""
        _, meta = bridge._format_exit_plan_mode({"plan": "test"})
        assert meta["questions"][0].get("is_plan_approval") is None or \
               meta["questions"][0].get("is_plan_approval") is False

    def test_plan_approval_marker_after_post(self):
        """_post_plan_approval が metadata に is_plan_approval を追加するシミュレーション"""
        _, meta = bridge._format_exit_plan_mode({"plan": "test"})
        # _post_plan_approval の処理を再現
        meta["questions"][0]["is_plan_approval"] = True
        assert meta["questions"][0]["is_plan_approval"] is True


# ── _normalize_tool_name: ツール名正規化 ──────────────────

class TestNormalizeToolName:
    """Bash 以外のツールの括弧付き引数を除去する正規化テスト"""

    def test_plain_tool_name(self):
        assert bridge.ClaudeCodeRunner._normalize_tool_name("Grep") == "Grep"

    def test_bash_pattern_preserved(self):
        """Bash(pattern) はそのまま保持"""
        assert bridge.ClaudeCodeRunner._normalize_tool_name("Bash(git *)") == "Bash(git *)"
        assert bridge.ClaudeCodeRunner._normalize_tool_name("Bash(npm install)") == "Bash(npm install)"

    def test_grep_path_stripped(self):
        """Grep(/path) はベース名のみ"""
        assert bridge.ClaudeCodeRunner._normalize_tool_name("Grep(/Users/foo/bar)") == "Grep"

    def test_read_path_stripped(self):
        assert bridge.ClaudeCodeRunner._normalize_tool_name("Read(/tmp/test.txt)") == "Read"

    def test_no_parens(self):
        assert bridge.ClaudeCodeRunner._normalize_tool_name("WebFetch") == "WebFetch"
