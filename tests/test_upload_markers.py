"""[SLACK_UPLOAD:path] マーカー解決と子プロセス env 除外のテスト。"""

import os
from unittest.mock import MagicMock

import bridge


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class TestResolveUploadMarkers:
    def test_relative_file_in_working_dir(self, tmp_path):
        _write(tmp_path / "out" / "thumb.png")
        paths, rejected = bridge._resolve_upload_markers(
            "done [SLACK_UPLOAD:out/thumb.png]", str(tmp_path))
        assert paths == [os.path.realpath(tmp_path / "out" / "thumb.png")]
        assert rejected == []

    def test_duplicates_collapsed(self, tmp_path):
        _write(tmp_path / "a.mp4")
        paths, _ = bridge._resolve_upload_markers(
            "[SLACK_UPLOAD:a.mp4] [SLACK_UPLOAD:a.mp4]", str(tmp_path))
        assert len(paths) == 1

    def test_outside_working_dir_rejected(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        _write(tmp_path / "secret.txt")
        paths, rejected = bridge._resolve_upload_markers(
            f"[SLACK_UPLOAD:../secret.txt] [SLACK_UPLOAD:{tmp_path / 'secret.txt'}]", str(work))
        assert paths == []
        assert len(rejected) == 2

    def test_symlink_escape_rejected(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        _write(tmp_path / "secret.txt")
        os.symlink(tmp_path / "secret.txt", work / "link.txt")
        paths, rejected = bridge._resolve_upload_markers("[SLACK_UPLOAD:link.txt]", str(work))
        assert paths == []
        assert rejected

    def test_env_and_git_rejected(self, tmp_path):
        _write(tmp_path / ".env")
        _write(tmp_path / ".env.local")
        _write(tmp_path / ".git" / "config")
        paths, rejected = bridge._resolve_upload_markers(
            "[SLACK_UPLOAD:.env] [SLACK_UPLOAD:.env.local] [SLACK_UPLOAD:.git/config]", str(tmp_path))
        assert paths == []
        assert len(rejected) == 3

    def test_missing_and_too_large(self, tmp_path, monkeypatch):
        _write(tmp_path / "big.bin", b"x" * 20)
        monkeypatch.setattr(bridge, "SLACK_UPLOAD_MAX_BYTES", 10)
        paths, rejected = bridge._resolve_upload_markers(
            "[SLACK_UPLOAD:nope.png] [SLACK_UPLOAD:big.bin]", str(tmp_path))
        assert paths == []
        assert len(rejected) == 2

    def test_max_files(self, tmp_path):
        markers = []
        for i in range(bridge.SLACK_UPLOAD_MAX_FILES + 2):
            _write(tmp_path / f"f{i}.png")
            markers.append(f"[SLACK_UPLOAD:f{i}.png]")
        paths, rejected = bridge._resolve_upload_markers(" ".join(markers), str(tmp_path))
        assert len(paths) == bridge.SLACK_UPLOAD_MAX_FILES
        assert len(rejected) == 2

    def test_no_markers(self, tmp_path):
        assert bridge._resolve_upload_markers("plain text", str(tmp_path)) == ([], [])


class TestChildEnvBlocklist:
    def test_tokens_blocked(self):
        for key in ("CLAUDECODE", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GITHUB_TOKEN"):
            assert key in bridge._CHILD_ENV_BLOCKLIST


def test_update_check_disabled_by_default():
    assert bridge.GITHUB_UPDATE_CHECK is False


def _bare_runner():
    runner = object.__new__(bridge.ClaudeCodeRunner)
    runner.client = MagicMock()
    return runner


class TestPostCompletionUpload:
    def test_marker_file_attached(self, tmp_path):
        _write(tmp_path / "out.png")
        runner = _bare_runner()
        session = bridge.Session(thread_ts="1.0", channel_id="C1", working_dir=str(tmp_path))
        task = bridge.Task(id=1, prompt="p", status=bridge.TaskStatus.COMPLETED,
                           result="できました [SLACK_UPLOAD:out.png]")
        runner._post_completion(session, task, "done", None)
        uploads = runner.client.files_upload_v2.call_args.kwargs["file_uploads"]
        assert {"file": os.path.realpath(tmp_path / "out.png"),
                "filename": "out.png", "title": "out.png"} in uploads

    def test_rejected_noted_in_blocks(self, tmp_path):
        runner = _bare_runner()
        session = bridge.Session(thread_ts="1.0", channel_id="C1", working_dir=str(tmp_path))
        task = bridge.Task(id=1, prompt="p", status=bridge.TaskStatus.COMPLETED,
                           result="[SLACK_UPLOAD:../x.png]")
        runner._post_completion(session, task, "done", None,
                                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "h"}}])
        blocks = runner.client.chat_postMessage.call_args.kwargs["blocks"]
        assert "x.png" in blocks[-1]["text"]["text"]


class TestBridgeDirDenyRules:
    def _disallowed(self, task):
        cmd = _bare_runner().build_command(task)
        return cmd[cmd.index("--disallowedTools") + 1]

    def test_default(self):
        value = self._disallowed(bridge.Task(id=1, prompt="p"))
        assert "AskUserQuestion" in value
        for tool in ("Read", "Edit", "Write"):
            assert f"{tool}(/{bridge.REPO_DIR}/**)" in value

    def test_kept_with_explicit_override(self):
        value = self._disallowed(bridge.Task(id=1, prompt="p", disallowed_tools=""))
        assert value == bridge._BRIDGE_DIR_DENY_RULES
