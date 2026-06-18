#!/usr/bin/env python3
"""
Claude Code ⇔ Slack Bridge
===========================
Mac上で動くClaude CodeをSlackから操作するブリッジ。
複数タスクの同時実行に対応。チャンネルモードのみ（@bot メンション必須）。

3層データモデル:
  Project (= Slack Channel) — セッションのコンテナ
  Session (= Slack Thread)  — Project 内で並列。タスクの直列チェーン
  Task    (= 指示→完了)     — Session 内で直列。スレッド返信で自動 --resume

コマンド（すべて @bot メンション付きで送信）:
  in <path> <タスク>        → 指定ディレクトリでタスクを実行
  fork <PID> [<タスク>]     → 実行中のclaude CLIプロセスをフォーク
  fork                      → フォーク可能なプロセス一覧
  <タスク内容>              → ディレクトリ選択画面からタスクを実行（root設定時は即実行）
  team [in <path>] <タスク> → Team Agentモードで並列実行
  root [<path>|clear]       → チャンネルのルートディレクトリ設定/表示/解除
  status                    → タスクの状態一覧
  sessions                  → セッション一覧
  cancel                    → タスクをキャンセル（スレッド内はそのセッション対象）
  tools <list>              → 次回タスクの許可ツール設定（スレッド内のみ）
  help                      → ヘルプ表示

スレッド返信（メンションなし）:
  <指示>                    → 同セッションで --resume 続行（タスク待機中はCLIに転送）
"""

import collections
import fcntl
import io
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import urlparse
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from i18n import t

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None


from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 設定（バリデーション付き）──────────────────────────────
def _validate_config():
    """必須環境変数とコマンドの存在を確認し、問題があればわかりやすいエラーを出す"""
    errors = []

    # 必須環境変数
    required_vars = {
        "SLACK_BOT_TOKEN": "Slack Bot Token (xoxb-...)",
        "SLACK_APP_TOKEN": "Slack App-Level Token (xapp-...)",
        "ADMIN_SLACK_USER_ID": "管理者の Slack User ID (U...)",
    }
    for var, desc in required_vars.items():
        val = os.environ.get(var, "")
        if not val:
            errors.append(f"  {var} が未設定です — {desc}")

    # トークン形式チェック（設定されている場合のみ）
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if bot_token and not bot_token.startswith("xoxb-"):
        errors.append(f"  SLACK_BOT_TOKEN が xoxb- で始まっていません (値: {bot_token[:10]}...)")

    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if app_token and not app_token.startswith("xapp-"):
        errors.append(f"  SLACK_APP_TOKEN が xapp- で始まっていません (値: {app_token[:10]}...)")

    admin_id = os.environ.get("ADMIN_SLACK_USER_ID", "")
    if admin_id and not admin_id.startswith("U"):
        errors.append(f"  ADMIN_SLACK_USER_ID が U で始まっていません (値: {admin_id})")

    # claude コマンドの存在チェック
    claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
    if shutil.which(claude_cmd) is None:
        errors.append(
            f"  claude コマンドが見つかりません (CLAUDE_CMD={claude_cmd})\n"
            f"    → Claude Code CLI をインストールしてください\n"
            f"    → 別のパスにある場合は .env の CLAUDE_CMD を設定してください"
        )

    if errors:
        logger.error("=" * 55)
        logger.error("  設定エラーが見つかりました:")
        logger.error("=" * 55)
        for e in errors:
            logger.error(e)
        logger.error("=" * 55)
        logger.error("  .env ファイルを確認してください")
        logger.error("  セットアップ: ./scripts/install.sh")
        logger.error("=" * 55)
        sys.exit(1)

_validate_config()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
# 管理者ユーザーID（必須）。アクセス制御に使用。
ADMIN_SLACK_USER_ID = os.environ["ADMIN_SLACK_USER_ID"]
# 許可ユーザー（カンマ区切り or "*" で全許可）
SLACK_ALLOWED_USERS = os.getenv("SLACK_ALLOWED_USERS", "")
# 許可チャンネル（カンマ区切り or "*" で全許可）
SLACK_ALLOWED_CHANNELS = os.getenv("SLACK_ALLOWED_CHANNELS", "")
# 通知チャンネル（起動/停止通知の送信先。未設定ならログのみ）
NOTIFICATION_CHANNEL = os.getenv("NOTIFICATION_CHANNEL", "")
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
DEFAULT_ALLOWED_TOOLS = os.getenv(
    "DEFAULT_ALLOWED_TOOLS",
    "Read,Write,Edit,MultiEdit,Bash(git *),TaskCreate,TaskUpdate,TaskList",
)

MAX_SLACK_MSG_LENGTH = 39000  # Slack API上限は約40,000文字（JSONエスケープ込み）
MAX_SLACK_FILE_SIZE = 20 * 1024 * 1024  # 20MB
DIRECTORY_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "directory_history.json")
CHANNEL_ROOTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_roots.json")
PROJECT_TOOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_tools.json")
DIRECTORY_HISTORY_MAX = 10
SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.json")
SESSIONS_MAX_AGE_DAYS = 7
SESSIONS_MAX_PER_CHANNEL = 50
# プロセス突然死時の進行中タスクをトラッキング。
# タスク開始/終了時にライブ書き換え。次回起動時に残っているエントリ = 中断タスク。
INTERRUPTED_TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "interrupted_tasks.json")

# App Home ダッシュボード — 終了済みセッションの薄い履歴（再起動後も保持）
SESSION_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_history.json")
SESSION_HISTORY_MAX = 100        # 全体で保持する終了済みセッションの最大件数
SESSION_HISTORY_DISPLAY = 30     # Home ビューに表示する終了済みセッションの最大件数
APP_HOME_POLL_INTERVAL = 30      # 進行中セッションがある間の再 publish 間隔（秒）
APP_HOME_VIEWER_TTL = 600        # app_home_opened を最後に受けてから再 publish 対象とみなす猶予（秒）

# ── GitHub 更新チェック ──
# 既定で1時間ごとに GitHub 上の最新版と手元の版を比較し、差があれば
# NOTIFICATION_CHANNEL に「アップデートがあります」とボタン付きで通知する。
GITHUB_UPDATE_CHECK = os.getenv("GITHUB_UPDATE_CHECK", "true").lower() in ("1", "true", "yes", "on")
GITHUB_UPDATE_INTERVAL = int(os.getenv("GITHUB_UPDATE_INTERVAL", "3600"))  # チェック間隔（秒）
GITHUB_REPO = os.getenv("GITHUB_UPDATE_REPO", "nariakiiwatani/claude-slack-bridge")
GITHUB_BRANCH = os.getenv("GITHUB_UPDATE_BRANCH", "main")
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
# 非git（zip等でDLした）インストール時、現在入っている版のSHAを記録する状態ファイル
GITHUB_UPDATE_STATE_FILE = os.path.join(REPO_DIR, "github_update_state.json")

# Team Agent用追加ツール
TEAM_EXTRA_TOOLS = "TeamCreate,TeamDelete,SendMessage,TaskCreate,TaskUpdate,TaskList,Agent"

# ツールリクエストマーカー検出用正規表現
_TOOL_REQUEST_RE = re.compile(r"\[TOOL_REQUEST:([^\]]+)\]")


# ---------------------------------------------------------------------------
# Slack 添付画像ダウンロード・正規化
# ---------------------------------------------------------------------------


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトを自動追跡しないハンドラ（手動でAuth付きリダイレクトを行うため）"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            newurl, code, msg, headers, fp
        )

_no_redirect_opener = urllib.request.build_opener(_NoAutoRedirect)


def _download_slack_file_content(url: str, token: str) -> bytes | None:
    """SlackのファイルURLからバイナリデータをダウンロードする。

    Python 3.11+ の urllib は異なるホストへのリダイレクト時に Authorization ヘッダーを
    セキュリティ上の理由で自動削除する。Slackのファイル URL は
    files.slack.com → <workspace>.slack.com 等にリダイレクトするため、
    リダイレクトを手動追跡し、各ステップで Authorization を付与する。
    """
    headers = {"Authorization": f"Bearer {token}"}

    for _attempt in range(5):
        req = urllib.request.Request(url, headers=headers)
        try:
            with _no_redirect_opener.open(req) as resp:
                data = resp.read()
                logger.info("Slack file fetched: status=%d, url=%s", resp.status, url[:80])
                # HTMLログインページが返された場合
                if data[:15] == b"<!DOCTYPE html>" or data[:5] == b"<html":
                    logger.error("Slack returned HTML instead of file (%d bytes). "
                                 "Check bot token files:read scope.", len(data))
                    return None
                return data
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                new_url = e.headers.get("Location", "")
                if not new_url:
                    logger.warning("Redirect missing Location header")
                    return None
                # 相対URLを絶対URLに変換
                if new_url.startswith("/"):
                    parsed = urlparse(url)
                    new_url = f"{parsed.scheme}://{parsed.netloc}{new_url}"
                logger.info("Following redirect (%d): %s -> %s", e.code, url[:60], new_url[:60])
                url = new_url
                continue
            logger.warning("Slack file download: HTTP %d", e.code)
            return None

    logger.warning("Slack file download: too many redirects")
    return None


# Claude API がサポートする画像形式
_API_SUPPORTED_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}
# 画像の長辺ピクセル上限（これを超えるとリサイズ）
_MAX_IMAGE_LONG_EDGE = 8000


def _normalize_image(path: str) -> str | None:
    """画像ファイルを検証し、APIが処理できる形式に正規化する。

    成功時は（変換後の）パスを返す。ファイルが無効な場合は None を返す。
    Pillow 未インストール時はそのまま返す。
    """
    if PILImage is None:
        return path

    try:
        img = PILImage.open(path)
        img.load()  # 実際にデコードして破損を検出
    except Exception:
        # ファイルの先頭バイトをログ出力（HTMLエラーページ等の診断用）
        try:
            with open(path, "rb") as f:
                head = f.read(64)
            logger.error("Cannot open image file: %s (first bytes: %r)", path, head)
        except Exception:
            logger.exception("Cannot open image file (read also failed): %s", path)
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    try:
        fmt = img.format
        w, h = img.size
        needs_convert = fmt not in _API_SUPPORTED_FORMATS
        needs_resize = max(w, h) > _MAX_IMAGE_LONG_EDGE

        if not needs_convert and not needs_resize:
            img.close()
            return path

        if needs_resize:
            ratio = _MAX_IMAGE_LONG_EDGE / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)

        # 保存形式を決定（透過があればPNG、それ以外はJPEG）
        if img.mode in ("RGBA", "LA", "P"):
            out_fmt, ext = "PNG", ".png"
        else:
            if img.mode != "RGB":
                img = img.convert("RGB")
            out_fmt, ext = "JPEG", ".jpg"

        base = os.path.splitext(path)[0]
        out_path = base + ext
        img.save(out_path, out_fmt, quality=95)
        new_w, new_h = img.size
        img.close()

        if out_path != path:
            try:
                os.remove(path)
            except OSError:
                pass

        logger.info("Image normalized: %s -> %s (%dx%d, %s)",
                    os.path.basename(path), os.path.basename(out_path), new_w, new_h, out_fmt)
        return out_path
    except Exception:
        logger.exception("Image conversion failed: %s", path)
        img.close()
        return path  # 変換失敗時は元ファイルをそのまま試す


def _resolve_event_files(event: dict, channel_id: str = "") -> list[dict]:
    """イベントからファイル情報を抽出する。event.filesが空の場合はAPI経由で再取得を試みる。

    タスク作成時にのみ呼ぶこと（毎メッセージでは呼ばない）。
    """
    files = event.get("files", [])
    if files:
        return files

    # event.filesが空の場合、ファイルが添付されている手がかりを探す
    file_ids = event.get("file_ids", [])
    blocks = event.get("blocks", [])
    has_upload = event.get("upload", False)
    subtype = event.get("subtype", "")

    # blocks内のfileブロックからfile_idを抽出
    if not file_ids:
        for block in blocks:
            if block.get("type") == "file":
                fid = block.get("file_id") or block.get("external_id")
                if fid:
                    file_ids.append(fid)

    has_file_hints = file_ids or has_upload or subtype == "file_share"
    if has_file_hints:
        logger.info("event.files empty but file hints found (subtype=%s, upload=%s, file_ids=%s, event_keys=%s)",
                    subtype, has_upload, file_ids, sorted(event.keys()))

    # file_idがあればSlack APIでファイル情報を取得
    if file_ids:
        logger.info("Fetching file info from file_ids=%s", file_ids)
        resolved = []
        for fid in file_ids:
            try:
                resp = slack_client.files_info(file=fid)
                if resp.get("ok"):
                    resolved.append(resp["file"])
            except Exception:
                logger.exception("files.info failed: %s", fid)
        if resolved:
            return resolved

    # conversations.historyでメッセージを再取得してファイル情報を確認
    # （新しいSlack APIではevent.filesが空でもAPIからは取得できる場合がある）
    msg_ts = event.get("ts", "")
    if msg_ts and channel_id:
        try:
            resp = slack_client.conversations_history(
                channel=channel_id,
                latest=msg_ts,
                inclusive=True,
                limit=1,
            )
            msgs = resp.get("messages", [])
            if msgs and msgs[0].get("ts") == msg_ts:
                refetched = msgs[0].get("files", [])
                if refetched:
                    logger.info("Got %d files from conversations.history: %s",
                                len(refetched),
                                [(f.get("name"), f.get("mimetype")) for f in refetched])
                    return refetched
        except Exception:
            logger.exception("conversations.history re-fetch failed")

    if has_file_hints:
        logger.warning("File hints found but could not retrieve files (event_keys=%s)", sorted(event.keys()))
    return []


def _download_slack_files(files: list[dict], save_dir: str) -> list[str]:
    """Slackの添付ファイルをダウンロードしローカルパスのリストを返す"""
    _IMAGE_FILETYPES = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "tiff"}

    def _is_image(f: dict) -> bool:
        mime = f.get("mimetype", "")
        if mime and mime.startswith("image/"):
            return True
        ft = f.get("filetype", "").lower()
        return ft in _IMAGE_FILETYPES

    if not files:
        return []

    attach_dir = os.path.join(save_dir, ".slack-attachments")
    os.makedirs(attach_dir, exist_ok=True)

    paths: list[str] = []
    for f in files:
        size = f.get("size", 0)
        if size > MAX_SLACK_FILE_SIZE:
            logger.warning("Attachment too large, skipping: %s (%d bytes)", f.get("name"), size)
            continue

        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            logger.warning("Attachment has no download URL: %s (keys=%s)", f.get("name"), list(f.keys()))
            continue

        # ファイル名のスペースをアンダースコアに置換（パス解釈の問題を防止）
        raw_name = f.get("name", "file").replace(" ", "_")
        filename = f"{int(time.time())}_{raw_name}"
        dest = os.path.join(attach_dir, filename)
        try:
            data = _download_slack_file_content(url, SLACK_BOT_TOKEN)
            if data is None:
                logger.warning("Attachment download failed (invalid response): %s", raw_name)
                continue
            with open(dest, "wb") as out:
                out.write(data)

            # 画像の場合は正規化（形式変換、リサイズ等）
            if _is_image(f):
                normalized = _normalize_image(dest)
                if normalized:
                    paths.append(normalized)
                    logger.info("Attachment image prepared: %s", normalized)
                else:
                    logger.warning("Attachment image normalization failed, skipping: %s", raw_name)
            else:
                paths.append(dest)
                logger.info("Attachment file prepared: %s", dest)
        except Exception:
            logger.exception("Attachment download failed: %s", raw_name)
    return paths


def _augment_prompt_with_files(prompt: str, file_paths: list[str]) -> str:
    """添付ファイルパスをプロンプト末尾に追記"""
    lines = "\n".join(file_paths)
    return f"{prompt}\n\n{t('prompt_attached_files')}\n{lines}"


def _is_user_allowed(user_id: str) -> bool:
    """ユーザーが操作を許可されているか（チャンネルモード用）"""
    if user_id == ADMIN_SLACK_USER_ID:
        return True  # 管理者は常に許可
    if SLACK_ALLOWED_USERS == "*":
        return True
    allowed = {u.strip() for u in SLACK_ALLOWED_USERS.split(",") if u.strip()}
    return user_id in allowed


def _is_channel_allowed(channel_id: str) -> bool:
    """チャンネルが許可されているか"""
    if SLACK_ALLOWED_CHANNELS == "*":
        return True
    allowed = {c.strip() for c in SLACK_ALLOWED_CHANNELS.split(",") if c.strip()}
    return channel_id in allowed


TASK_LABELS = [
    ("🔵", "blue"),
    ("🟢", "green"),
    ("🟡", "yellow"),
    ("🟣", "purple"),
    ("🟠", "orange"),
]


# ── タスク進捗リアクション（差し替え式） ────
REACTION_RECEIVED = "eyes"
REACTION_RUNNING = "gear"
REACTION_AWAITING_TOOL = "bell"
REACTION_COMPLETED = "white_check_mark"
REACTION_FAILED = "x"
REACTION_CANCELLED = "stop_sign"


def _build_reaction_targets(channel_id: str,
                            request_msg_ts: str | None,
                            thread_ts: str | None) -> list:
    """進捗リアクションの付与対象 (channel_id, msg_ts) リストを構築。
    - ユーザー指示メッセージ（あれば）
    - スレッド返信トリガなら、スレッド親も追加（指示メッセージと別物のとき）"""
    targets: list = []
    if request_msg_ts:
        targets.append((channel_id, request_msg_ts))
    if thread_ts and thread_ts != request_msg_ts:
        targets.append((channel_id, thread_ts))
    return targets


def _task_anchor_ts(task: "Task") -> Optional[str]:
    """タスクの基点メッセージ ts（= ユーザー指示メッセージ）を返す。
    App Home のリンクをスレッド先頭ではなく最新タスクへ向けるために使う。"""
    if task and task.reaction_targets:
        # 先頭 = (channel_id, ユーザー指示メッセージ ts)
        return task.reaction_targets[0][1]
    return None


# ── データ構造（3層モデル: Project → Session → Task） ────
class TaskStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """1回のClaude Code呼び出し。Session内で直列実行される。"""
    id: int
    prompt: str
    status: TaskStatus = TaskStatus.QUEUED
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    tool_calls: list = field(default_factory=list)
    allowed_tools: Optional[str] = None
    disallowed_tools: Optional[str] = None  # None=デフォルト(AskUserQuestion,ExitPlanMode), 明示値で上書き
    user_id: Optional[str] = None
    # トークン使用量（JSONLのassistantエントリから累積）
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # ファイル変更差分（Edit/Write検出時に蓄積）
    file_diffs: list = field(default_factory=list)

    # プロセス管理（実行中のみ）
    process: Optional[subprocess.Popen] = None
    master_fd: Optional[int] = None
    # 最終JSONL活動時刻。プロセス生存でも JSONL 無活動が続けば「ハング」とみなす判断に使う
    # （socket wedge 時の再起動ガードがハングタスクに人質に取られるのを防ぐ）。
    last_activity: float = field(default_factory=time.time)

    # セッション継続（Session.claude_session_id から自動設定）
    resume_session: Optional[str] = None
    fork_session: bool = False  # True: --fork-session 付きで起動（元セッションに影響を与えない分岐）

    # 進捗リアクション対象（channel_id, msg_ts のペアのリスト）
    # ユーザー指示メッセージ + (スレッド返信なら) スレッド親 を入れる
    reaction_targets: list = field(default_factory=list)
    # 現在付与中のリアクション名（差し替え時の remove に使用）
    current_reaction: Optional[str] = None

    @property
    def short_id(self) -> str:
        return f"#{self.id}"


@dataclass
class Session:
    """Slackスレッド = 1セッション。Project内で並列、タスクは直列チェーン。"""
    thread_ts: str                    # Slackスレッド親ts = 識別子
    channel_id: str                   # 所属チャンネル
    claude_session_id: Optional[str] = None  # Claude CLIのsession_id
    label_emoji: str = ""
    label_name: str = ""
    working_dir: Optional[str] = None  # in コマンドによるオーバーライド
    created_at: Optional[datetime] = None
    tasks: list[Task] = field(default_factory=list)
    next_tools: Optional[str] = None  # tools コマンドで設定、次タスク実行時に消費
    approved_tools: set[str] = field(default_factory=set)  # セッション単位で承認済みツール
    pending_question: Optional[dict] = None  # プロセス終了後の --resume 用質問メタデータ
    pending_tool_request: Optional[dict] = None  # ツール許可リクエスト {"tools": [...], "user_id": "..."}
    pending_fork: bool = False  # True: 次のタスク実行時に --fork-session を付与（1回消費）
    prompts: list[str] = field(default_factory=list)  # ユーザー指示の履歴（永続化）
    pending_followup: list[dict] = field(default_factory=list)  # 実行中に受けた追加指示のキュー。タスク完了時(_executeのfinally)に順次 --resume 発火。各要素: {text, user_id, files, request_msg_ts, mode("queue"|"cancel")}

    @property
    def active_task(self) -> Optional[Task]:
        """実行中のタスクを返す"""
        for t in self.tasks:
            if t.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                return t
        return None

    @property
    def latest_task(self) -> Optional[Task]:
        """最後のタスクを返す"""
        return self.tasks[-1] if self.tasks else None

    @property
    def display_label(self) -> str:
        return f"{self.label_emoji} {self.label_name}" if self.label_name else self.label_emoji

    def consume_tools(self) -> Optional[str]:
        """next_tools を取り出してリセット"""
        tools = self.next_tools
        self.next_tools = None
        return tools

    def consume_fork(self) -> bool:
        """pending_fork を取り出してリセット"""
        if self.pending_fork:
            self.pending_fork = False
            return True
        return False


@dataclass
class Project:
    """Slackチャンネル = 1プロジェクト。セッションのコンテナ。"""
    channel_id: str                   # Slackチャンネル = 識別子
    root_dir: Optional[str] = None    # チャンネルのルートディレクトリ（永続化あり）
    approved_tools: set[str] = field(default_factory=set)  # プロジェクト単位で承認済みツール
    sessions: dict[str, Session] = field(default_factory=dict)

    def assign_label(self, session: Session):
        """ラベル割り当て（プロジェクトスコープ：アクティブセッション間で重複なし）"""
        used = set()
        for s in self.sessions.values():
            if s.active_task and s.label_name:
                used.add(s.label_name)
        for emoji, name in TASK_LABELS:
            if name not in used:
                session.label_emoji = emoji
                session.label_name = name
                return
        session.label_emoji = "⚪"
        session.label_name = f"session-{session.thread_ts[:8]}"

    def get_or_create_session(self, thread_ts: str) -> Session:
        """既存セッションを返すか、新規作成する"""
        if thread_ts not in self.sessions:
            session = Session(
                thread_ts=thread_ts,
                channel_id=self.channel_id,
                created_at=datetime.now(),
            )
            self.assign_label(session)
            self.sessions[thread_ts] = session
        return self.sessions[thread_ts]

    def find_task_by_id(self, task_id: int) -> Optional[tuple["Session", Task]]:
        """タスクIDからセッションとタスクを検索"""
        for session in self.sessions.values():
            for task in session.tasks:
                if task.id == task_id:
                    return (session, task)
        return None

    def find_session_by_claude_id(self, claude_session_id: str) -> Optional["Session"]:
        """Claude CLIのsession_idからセッションを検索（部分一致対応）"""
        for session in self.sessions.values():
            if session.claude_session_id and session.claude_session_id.startswith(claude_session_id):
                return session
        return None

    @property
    def active_sessions(self) -> list["Session"]:
        """アクティブタスクを持つセッション一覧"""
        return [s for s in self.sessions.values() if s.active_task]

    @property
    def all_tasks(self) -> list[Task]:
        """全タスク一覧"""
        tasks = []
        for session in self.sessions.values():
            tasks.extend(session.tasks)
        return tasks


# ── 実行中のclaude CLIプロセス検出 ───────────────────────
def detect_running_claude_instances() -> list[dict]:
    """Mac上で動作中のclaude CLIプロセスを検出し、PID・CWD・TTY・経過時間を返す"""
    instances = []
    try:
        # ps でコマンドが "claude" のプロセスを検出（PID, TTY, COMM）
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,comm"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        tty_map = {}
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and os.path.basename(parts[2]) == "claude":
                pid = int(parts[0])
                tty = parts[1]  # e.g., "ttys021"
                pids.append(pid)
                tty_map[pid] = tty
        if not pids:
            return []

        for pid in pids:
            etime = _get_process_etime(pid)
            cwd = _get_process_cwd(pid)
            tty = tty_map.get(pid, "??")
            instances.append({
                "pid": pid,
                "cwd": cwd or "(unknown)",
                "etime": etime or "?",
                "tty": tty,
            })
    except Exception as e:
        logger.warning("Claude process detection error: %s", e)
    return instances


def _get_process_etime(pid: int) -> Optional[str]:
    """プロセスの経過時間を取得 (例: "02:15:30", "1-03:22:10")"""
    try:
        result = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _get_process_cwd(pid: int) -> Optional[str]:
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-F", "n"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n/"):
                return line[1:]
    except Exception:
        pass
    return None


# ── ターミナル出力モニタリング ─────────────────────────────
_PERIODIC_CHECK_INTERVAL = 15.0  # プロセス生存・テイクオーバー等の定期チェック間隔（秒）


class _JsonlWatcher:
    """mtimeポーリング + threading.Event でJSONLファイル変更を検知。
    notify() で外部イベント（Slack入力転送等）を即座に通知でき、
    ファイル変更は短周期の mtime チェックで検知する。"""

    _POLL_INTERVAL = 1.0  # mtime チェック間隔（秒）

    def __init__(self):
        self._event = threading.Event()       # monitor thread への通知用
        self._stop_event = threading.Event()  # ポーリングスレッド停止用
        self._paths: list[str] = []
        self._mtimes: dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self, paths: list[str]):
        """監視対象パスを設定してポーリングスレッドを開始。"""
        for p in paths:
            if p:
                self._paths.append(p)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def add_path(self, path: str):
        """監視対象パスを追加。"""
        if path and path not in self._paths:
            self._paths.append(path)

    def wait(self, timeout: float) -> bool:
        """変更イベントまたはnotifyを待つ。変更があればTrue、タイムアウトならFalse。"""
        result = self._event.wait(timeout=timeout)
        self._event.clear()
        return result

    def notify(self):
        """外部から手動でイベントを発火（入力メッセージ到着時など）。"""
        self._event.set()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _poll_loop(self):
        """バックグラウンドでmtimeを定期チェックし、変更があればイベントを発火。"""
        while not self._stop_event.is_set():
            changed = False
            for p in list(self._paths):
                try:
                    if os.path.isdir(p):
                        for root, _dirs, files in os.walk(p):
                            for f in files:
                                if f.endswith(".jsonl"):
                                    fp = os.path.join(root, f)
                                    try:
                                        mt = os.path.getmtime(fp)
                                        if fp not in self._mtimes:
                                            self._mtimes[fp] = mt
                                            changed = True
                                        elif mt > self._mtimes[fp]:
                                            self._mtimes[fp] = mt
                                            changed = True
                                    except OSError:
                                        pass
                    else:
                        try:
                            mt = os.path.getmtime(p)
                            if p not in self._mtimes:
                                self._mtimes[p] = mt
                            elif mt > self._mtimes[p]:
                                self._mtimes[p] = mt
                                changed = True
                        except OSError:
                            pass
                except OSError:
                    pass
            if changed:
                self._event.set()
            self._stop_event.wait(timeout=self._POLL_INTERVAL)


def _read_terminal_contents(tty_device: str) -> Optional[str]:
    """Terminal.appのAppleScript APIでタブのスクロールバック+表示内容を取得"""
    script = f'''
tell application "Terminal"
    set targetTTY to "{tty_device}"
    repeat with i from 1 to count of windows
        set w to window i
        repeat with j from 1 to count of tabs of w
            if tty of tab j of w is targetTTY then
                return history of tab j of w
            end if
        end repeat
    end repeat
end tell
return ""
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception:
        return None


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # ゾンビプロセス検出: os.kill(0) はゾンビでも成功するため、
    # waitpid(WNOHANG) でゾンビかどうか確認する
    try:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
        if waited_pid != 0:
            return False  # ゾンビだった（reap済み）
    except ChildProcessError:
        # 自プロセスの子でない場合は waitpid が使えない
        # /proc が使えないmacOSではpsコマンドで確認
        try:
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            )
            stat = result.stdout.strip()
            if stat.startswith("Z"):
                return False
        except Exception:
            pass
    return True


# ── セッションJSONL監視 ──────────────────────────────────
def _cwd_to_project_dir_name(cwd: str) -> str:
    """CWDをClaude CLIのプロジェクトディレクトリ名に変換。
    CLIは英数字・ハイフン・ドット以外の文字（/, _, スペース, 日本語等）を
    すべて '-' に置換する。"""
    return re.sub(r"[^a-zA-Z0-9\-.]", "-", cwd)


def _find_session_jsonl(cwd: str, min_mtime: float = 0, min_ctime: float = 0, exclude_paths: set[str] | None = None) -> Optional[str]:
    """CWDからclaude CLIのセッションJSONLファイルパスを特定。
    JONLファイル内のcwdフィールドで逆引きマッチする。
    min_ctime: ファイル作成時刻(st_birthtime)がこの値以降のもののみ対象（macOS用）。
    exclude_paths: 除外するファイルパスのセット（重複監視防止用）。"""
    if not cwd or cwd == "(unknown)":
        return None
    projects_base = Path.home() / ".claude" / "projects"
    if not projects_base.is_dir():
        return None

    # cwdからプロジェクトディレクトリを特定して検索範囲を絞る
    proj_dir_name = _cwd_to_project_dir_name(cwd)
    target_dir = projects_base / proj_dir_name
    if target_dir.is_dir():
        proj_dirs = [target_dir]
    else:
        # フォールバック: ディレクトリ名変換規則が合わない場合は全スキャン
        logger.debug("_find_session_jsonl: project dir not found for cwd=%s (expected=%s), falling back to full scan", cwd, proj_dir_name)
        proj_dirs = [d for d in projects_base.iterdir() if d.is_dir()]

    best_path: Optional[str] = None
    best_mtime: float = 0
    for proj_dir in proj_dirs:
        jsonl_files = list(proj_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue
        # min_ctimeが指定されている場合、作成時刻でフィルタ
        if min_ctime:
            jsonl_files = [f for f in jsonl_files
                           if getattr(f.stat(), 'st_birthtime', f.stat().st_mtime) >= min_ctime]
            if not jsonl_files:
                continue
        # mtime降順でソートし、候補を順番に検査
        jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        for latest in jsonl_files:
            fpath = str(latest)
            if exclude_paths and fpath in exclude_paths:
                continue
            mtime = latest.stat().st_mtime
            if mtime < min_mtime:
                break  # 以降はさらに古いのでスキップ
            # JONLの先頭数行からcwdを確認（file-history-snapshot等はcwdなし）
            try:
                matched = False
                with open(latest, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        if i >= 5:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        entry_cwd = entry.get("cwd", "")
                        if entry_cwd == cwd:
                            matched = True
                            break
                        elif entry_cwd:
                            break  # 別のcwdなので不一致
                if matched and mtime > best_mtime:
                    best_mtime = mtime
                    best_path = fpath
                    break  # このプロジェクトディレクトリ内で最適な候補が見つかった
            except OSError:
                continue
    return best_path


def _find_jsonl_path_for_session_id(session_id: str) -> Optional[str]:
    """session_idからJSONLファイルパスを直接特定。~/.claude/projects/*/<session_id>.jsonl を検索。"""
    projects_base = Path.home() / ".claude" / "projects"
    if not projects_base.is_dir():
        return None
    for proj_dir in projects_base.iterdir():
        if not proj_dir.is_dir():
            continue
        jsonl = proj_dir / f"{session_id}.jsonl"
        if jsonl.exists():
            return str(jsonl)
    return None


def _classify_jsonl_entry(entry: dict) -> list[tuple[str, str, dict | None]]:
    """JSONLエントリを (category, text, metadata) のリストに分類。
    category: "status" (thinking/tool_use), "text" (応答テキスト),
              "question" (AskUserQuestion選択肢), None (スキップ)
    metadata: "question"の場合に質問データを格納、それ以外はNone"""
    entry_type = entry.get("type", "")
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return []

    role = msg.get("role", "")
    content = msg.get("content", [])
    if not isinstance(content, list):
        return []

    results = []
    for c in content:
        if not isinstance(c, dict):
            continue
        c_type = c.get("type", "")

        if entry_type == "assistant" and c_type == "thinking":
            text = c.get("thinking", "")
            if text:
                # コードブロック内のバッククォート3連をエスケープ
                escaped = text.replace("```", "` ` `")
                results.append(("status", f":thought_balloon: thinking... ({len(text)}文字)\n```\n{escaped}\n```", None))
            else:
                results.append(("status", t("status_thinking", chars=0), None))

        elif entry_type == "assistant" and c_type == "text":
            text = c.get("text", "").strip()
            if text:
                results.append(("text", text, None))

        elif entry_type == "assistant" and c_type == "tool_use":
            tool_name = c.get("name", "?")
            tool_input = c.get("input", {})

            # AskUserQuestion検出
            if tool_name == "AskUserQuestion":
                formatted, meta = _format_ask_user_question(tool_input)
                if formatted:
                    results.append(("question", formatted, meta))
                    continue

            # ExitPlanMode検出（プラン承認質問として表示）
            if tool_name == "ExitPlanMode":
                formatted, meta = _format_exit_plan_mode(tool_input)
                if formatted:
                    results.append(("question", formatted, meta))
                    continue

            # Task/Agent（サブエージェント）表示改善
            if tool_name in ("Task", "Agent"):
                if isinstance(tool_input, dict):
                    desc = tool_input.get("description", "")
                    stype = tool_input.get("subagent_type", "")
                    team = tool_input.get("team_name", "")
                    name = tool_input.get("name", "")
                else:
                    desc, stype, team, name = "", "", "", ""
                if team and name:
                    # チームメイト表示
                    label = t("team_subagent_label", name=name, agent_type=stype or "agent", desc=desc)
                    results.append(("status", label, None))
                else:
                    label = f"{stype}: {desc}" if stype and desc else (desc or stype or "subagent")
                    results.append(("status", f":robot_face: `{tool_name}` {label}", None))
                continue

            summary = _summarize_input(tool_input) if isinstance(tool_input, dict) else str(tool_input)[:80]
            if summary:
                results.append(("status", f":wrench: `{tool_name}` {summary}", None))
            else:
                results.append(("status", f":wrench: `{tool_name}`", None))

    return results


def _format_ask_user_question(tool_input: dict) -> tuple[str, dict | None]:
    """AskUserQuestionのinputをSlack表示用テキストとメタデータに変換。
    返り値: (formatted_text, metadata) — パース失敗時は ("", None)"""
    questions = tool_input.get("questions", [])
    if not isinstance(questions, list) or not questions:
        return ("", None)

    all_parts = []
    all_questions_meta = []

    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question", "")
        options = q.get("options", [])
        multi_select = q.get("multiSelect", False)

        if not question_text or not isinstance(options, list):
            continue

        lines = [f":question: *{question_text}*"]

        option_items = []
        for i, opt in enumerate(options, 1):
            if not isinstance(opt, dict):
                continue
            label = opt.get("label", "")
            desc = opt.get("description", "")
            if desc:
                lines.append(f"  {i}. {label} — {desc}")
            else:
                lines.append(f"  {i}. {label}")
            option_items.append({"label": label, "description": desc})

        if multi_select:
            lines.append(t("question_multi_select_unsupported"))
        else:
            lines.append(t("question_reply_with_number"))

        all_parts.append("\n".join(lines))
        all_questions_meta.append({
            "question": question_text,
            "options": option_items,
            "multi_select": multi_select,
        })

    if not all_parts:
        return ("", None)

    formatted = "\n\n".join(all_parts)
    metadata = {"questions": all_questions_meta}
    return (formatted, metadata)


def _format_exit_plan_mode(tool_input: dict) -> tuple[str, dict | None]:
    """ExitPlanModeをプラン承認質問として整形。
    プラン内容は metadata["plan_content"] に格納（質問メッセージにはインライン表示しない）。"""
    plan_text = ""
    if isinstance(tool_input, dict):
        # 既知のフィールドを順に探す
        for key in ("plan", "content", "summary"):
            val = tool_input.get(key, "")
            if isinstance(val, str) and val.strip():
                plan_text = val.strip()
                break
        # allowedPrompts の表示
        prompts = tool_input.get("allowedPrompts", [])
        if isinstance(prompts, list) and prompts:
            prompt_lines = []
            for p in prompts:
                if isinstance(p, dict):
                    prompt_lines.append(f"- `{p.get('tool', '?')}`: {p.get('prompt', '')}")
            if prompt_lines:
                plan_text += "\n\n" + t("question_allowed_prompts") + "\n" + "\n".join(prompt_lines)

    lines = [t("question_plan_approval_required")]
    lines.append(f"  1. {t('question_approve_execute')}")
    lines.append(f"  2. {t('question_reject_feedback')}")
    lines.append(t("question_reply_with_feedback"))

    metadata = {
        "questions": [{
            "options": [
                {"label": t("question_approve_execute"), "description": ""},
                {"label": t("question_reject_feedback"), "description": ""},
            ],
            "multi_select": False,
        }],
        "plan_content": plan_text,
    }
    return ("\n".join(lines), metadata)


def _read_new_jsonl_entries(jsonl_path: str, file_offset: int) -> tuple[list[dict], int]:
    """JONLファイルからfile_offset以降の新しいエントリを読み取る。
    (entries, new_offset) を返す。"""
    entries = []
    try:
        current_size = os.path.getsize(jsonl_path)
    except OSError:
        return entries, file_offset
    if current_size <= file_offset:
        return entries, file_offset
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            f.seek(file_offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            file_offset = f.tell()
    except OSError:
        pass
    return entries, file_offset


def _read_subagent_jsonl_entries(
    subagents_dir: str,
    offsets: dict[str, int],
) -> list[tuple[str, dict]]:
    """サブエージェントJSONLファイルから新しいエントリを読み取る。
    返り値: [(agent_label, entry), ...]
    offsets: {filepath: byte_offset} — 読み取り済み位置を追跡（呼出側で保持）
    """
    if not subagents_dir or not os.path.isdir(subagents_dir):
        return []
    results = []
    try:
        files = [f for f in os.listdir(subagents_dir) if f.startswith("agent-") and f.endswith(".jsonl")]
    except OSError:
        return []
    for fname in files:
        fpath = os.path.join(subagents_dir, fname)
        if fpath not in offsets:
            offsets[fpath] = 0
        entries, new_offset = _read_new_jsonl_entries(fpath, offsets[fpath])
        offsets[fpath] = new_offset
        for entry in entries:
            agent_id = entry.get("message", {}).get("agentId", "") if isinstance(entry.get("message"), dict) else ""
            if not agent_id:
                agent_id = entry.get("agentId", "")
            label = agent_id[:6] if agent_id else fname.replace("agent-", "").replace(".jsonl", "")[:6]
            results.append((label, entry))
    return results


def _monitor_session_jsonl(inst: dict, thread_ts: str, channel: str, client: WebClient):
    """セッションJSONLファイルをwatchdogで監視し、新しいエントリをSlackに投稿。
    - thinking/tool_use → 1つのステータスメッセージをchat_updateで更新（:hourglass:付き）
      thinkingが来るたびに表示をリセットし、最新の1セット（thinking+後続tool_use）のみ表示。
      全履歴はall_status_historyに蓄積し、完了時にファイル添付で投稿。
    - text → 新しいメッセージとして投稿（応答テキスト）
    """
    pid = inst["pid"]
    jsonl_path = inst["jsonl_path"]
    task_ref = inst.get("task")  # bridge起動タスクの場合、Taskオブジェクト参照
    display_prefix = inst.get("display_prefix", f"PID {pid}")
    skip_exit = inst.get("skip_exit_message", False)
    fixed_jsonl = inst.get("fixed_jsonl", False)
    file_offset = 0

    # 既存の内容をスキップ（外部インスタンス用、bridge起動タスクは先頭から読む）
    if not inst.get("start_from_beginning"):
        try:
            file_offset = os.path.getsize(jsonl_path)
        except OSError:
            pass

    status_msg_ts: Optional[str] = None  # ステータスメッセージ（thinking/tool_use）
    status_lines: list[str] = []          # ステータス行の蓄積
    all_status_history: list[str] = []    # 全ステータス履歴（完了時スニペット用）
    last_posted_text: Optional[str] = None  # 重複投稿防止用
    latest_text: Optional[str] = None  # 最新のテキスト応答（進捗メッセージ内に表示）
    last_jsonl_update: float = time.time()  # 最終JSONL更新時刻（ハートビート表示用）
    _HEARTBEAT_INTERVAL = 30  # ハートビート更新間隔（秒）
    _last_heartbeat: float = 0  # 前回ハートビート更新時刻

    # サブエージェント監視
    jsonl_dir = os.path.dirname(jsonl_path) if jsonl_path else ""
    session_id_for_subagents = os.path.splitext(os.path.basename(jsonl_path))[0] if jsonl_path else ""
    subagents_dir = os.path.join(jsonl_dir, session_id_for_subagents, "subagents") if jsonl_path else ""
    subagent_offsets: dict[str, int] = {}
    # resume時: 既存サブエージェントJSONLの末尾から開始（前タスクのエントリを再投稿しない）
    if not inst.get("start_from_beginning") and subagents_dir and os.path.isdir(subagents_dir):
        try:
            for _fn in os.listdir(subagents_dir):
                if _fn.startswith("agent-") and _fn.endswith(".jsonl"):
                    _fp = os.path.join(subagents_dir, _fn)
                    try:
                        subagent_offsets[_fp] = os.path.getsize(_fp)
                    except OSError:
                        pass
        except OSError:
            pass

    # watchdog ファイル監視
    watcher = _JsonlWatcher()
    watch_paths = [p for p in [jsonl_path, subagents_dir] if p]
    watcher.start(watch_paths)
    inst["_watcher"] = watcher  # 外部から notify() するために inst に格納

    # プラン承認バッファリング（概要テキストを先に表示するため）
    pending_plan_approval: list | None = None  # [question_text, metadata, wait_start]

    def _extract_task_info(entry: dict):
        """エントリからtask情報（session_id, result, tool_calls）を抽出"""
        session_ref = inst.get("session")  # Session オブジェクト参照
        entry_type = entry.get("type", "")
        msg = entry.get("message", {})
        # session_id → Session に設定（task_ref がなくても実行）
        if isinstance(msg, dict):
            sid = msg.get("sessionId") or entry.get("sessionId")
        else:
            sid = entry.get("sessionId")
        if sid and session_ref:
            logger.debug("JSONL monitor: session_id acquired sid=%s thread=%s", sid[:16] if sid else None, thread_ts)
            session_ref.claude_session_id = sid
            runner.save_sessions()
        if not task_ref:
            return
        # result type
        if entry_type == "result":
            result_data = entry.get("result", {})
            result_text = ""
            if isinstance(result_data, dict):
                for content in result_data.get("content", []):
                    if isinstance(content, dict) and content.get("type") == "text":
                        result_text += content["text"]
            elif isinstance(result_data, str):
                result_text = result_data
            if result_text:
                task_ref.result = result_text
        # tool_calls, text, and token usage from assistant messages
        if entry_type == "assistant" and isinstance(msg, dict):
            # トークン使用量の累積
            usage = msg.get("usage", {})
            if isinstance(usage, dict):
                task_ref.input_tokens += usage.get("input_tokens", 0)
                task_ref.output_tokens += usage.get("output_tokens", 0)
                task_ref.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                task_ref.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            for c in msg.get("content", []):
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    tool_name = c.get("name", "?")
                    tool_input = c.get("input", {})
                    task_ref.tool_calls.append({
                        "name": tool_name,
                        "input": _summarize_input(tool_input) if isinstance(tool_input, dict) else str(tool_input)[:80],
                    })
                    # Edit/MultiEdit のdiff蓄積
                    if isinstance(tool_input, dict) and tool_name in ("Edit", "MultiEdit"):
                        _capture_edit_diff(task_ref, tool_name, tool_input)
                elif c.get("type") == "text":
                    text = c.get("text", "").strip()
                    if text:
                        task_ref.result = text

    def _collapse_status_lines(lines: list[str]) -> str:
        """ステータス行を折りたたみ表示: 完了済みツールはコンパクトに、最新のみ詳細表示"""
        if not lines:
            return ""
        # thinkingとtool行を分離
        thinking_line = None
        tool_lines = []
        for line in lines:
            if line.startswith(":thought_balloon:"):
                thinking_line = line
            else:
                tool_lines.append(line)
        parts = []
        if thinking_line:
            parts.append(thinking_line)
        if len(tool_lines) <= 2:
            # 少数ならそのまま表示
            parts.extend(tool_lines)
        else:
            # 完了済み（最後以外）はツール名だけコンパクトに表示
            completed_names = []
            for line in tool_lines[:-1]:
                # `:wrench: `ToolName` summary` or `:robot_face: `Task` label` 形式からツール名を抽出
                m = re.search(r"`([^`]+)`", line)
                if m:
                    completed_names.append(f"`{m.group(1)}`")
                elif line.startswith("  ↳"):
                    continue  # サブエージェント行はスキップ
                else:
                    completed_names.append("...")
            if completed_names:
                parts.append(f":white_check_mark: {' '.join(completed_names)} ({len(completed_names)})")
            # 最新のツール行はフル表示
            parts.append(tool_lines[-1])
        return "\n".join(parts)

    def _flush_progress(final: bool = False):
        """進捗メッセージを更新（テキスト応答 + ステータス行を1メッセージに統合）。
        Claudeのテキスト応答は markdown ブロック（表/リスト/コードが標準Markdownで描画）、
        ステータス行 + 実行中サフィックスは section/mrkdwn ブロックで投稿。
        final=Trueで⏳除去。"""
        nonlocal status_msg_ts
        if not status_lines and not latest_text:
            return
        if final:
            suffix = ""
        else:
            # 進捗バー風サフィックス: ツール使用回数サマリーを付与
            progress_extra = ""
            if task_ref and task_ref.tool_calls:
                from collections import Counter
                tool_counts = Counter(tc["name"] for tc in task_ref.tool_calls)
                top_tools = " ".join(f"{name}({cnt})" for name, cnt in tool_counts.most_common(5))
                elapsed = (datetime.now() - task_ref.started_at).total_seconds() if task_ref.started_at else 0
                progress_extra = f"  |  {elapsed:.0f}s · {top_tools}"
            # 最終JSONL更新からの経過時間を表示（10秒以上経過時）
            idle_secs = time.time() - last_jsonl_update
            if idle_secs >= 10:
                idle_str = f"{int(idle_secs)}s" if idle_secs < 60 else f"{int(idle_secs // 60)}m{int(idle_secs % 60):02d}s"
                progress_extra += f"  |  {t('status_last_update', idle=idle_str)}"
            suffix = t("status_running") + progress_extra

        blocks: list[dict] = []
        fallback_parts: list[str] = []

        # Claudeのテキスト応答: markdownブロック（1ペイロード合計12,000文字制限。margin入れて11,000で切り詰め）
        if latest_text:
            md_text = latest_text
            if len(md_text) > 11000:
                md_text = md_text[:11000] + "\n" + t("status_continued")
            blocks.append({
                "type": "markdown",
                "text": f":speech_balloon: **{display_prefix}**\n\n{md_text}",
            })
            fallback_parts.append(f":speech_balloon: {display_prefix}\n{md_text[:300]}")

        # ステータス行 + 実行中サフィックス: section/mrkdwn（1ブロック3,000文字制限）
        status_text = _collapse_status_lines(status_lines) if status_lines else ""
        max_status = max(500, 2800 - len(suffix))
        if len(status_text) > max_status:
            status_text = status_text[:max_status] + "\n..."
        combined = f"{status_text}\n{suffix}" if status_text and suffix else (status_text or suffix)
        if combined:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": combined},
            })
            fallback_parts.append(combined)

        fallback_text = "\n\n".join(fallback_parts)
        if len(fallback_text) > 3000:
            fallback_text = fallback_text[:3000] + "\n..."
        try:
            if status_msg_ts:
                client.chat_update(
                    channel=channel, ts=status_msg_ts,
                    text=fallback_text, blocks=blocks,
                )
            else:
                resp = client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=fallback_text, blocks=blocks,
                )
                status_msg_ts = resp["ts"]
        except Exception as e:
            # msg_too_longエラー時は積極的に切り詰めてリトライ（blocksは除去）
            if "msg_too_long" in str(e) and len(fallback_text) > 2000:
                fallback_text = fallback_text[:2000] + "\n..."
                try:
                    if status_msg_ts:
                        client.chat_update(channel=channel, ts=status_msg_ts, text=fallback_text, blocks=[])
                    else:
                        resp = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=fallback_text, blocks=[])
                        status_msg_ts = resp["ts"]
                except Exception as e2:
                    logger.error("JSONL progress update retry error PID %d: %s", pid, e2)
            else:
                logger.error("JSONL progress update error PID %d: %s", pid, e)

    def _finalize_progress():
        """進捗メッセージを確定（⏳除去）。メッセージは保持して次回も同じメッセージに追記。"""
        if status_msg_ts and (status_lines or latest_text):
            _flush_progress(final=True)

    def _update_text(text: str):
        """応答テキストを進捗メッセージに統合表示。同一テキストの重複更新を防止。"""
        nonlocal last_posted_text, latest_text
        if text == last_posted_text:
            return  # 同一テキストの重複更新を防止
        last_posted_text = text
        latest_text = text  # 進捗メッセージ内に表示
        # テキスト応答も履歴に追加（完了時スニペットに含めるため）
        all_status_history.append(f"💬\n{text}")
        # 進捗メッセージを更新（新規メッセージは投稿しない）
        _flush_progress()

    def _post_question(text: str, metadata: dict | None, extra_files: list[dict] | None = None):
        """AskUserQuestion の選択肢をスレッドに投稿し、pending_questionを設定。
        進捗メッセージを削除し、進捗履歴をファイルとして添付。"""
        nonlocal status_msg_ts
        # 進捗メッセージを削除
        if status_msg_ts:
            try:
                client.chat_delete(channel=channel, ts=status_msg_ts)
            except Exception as e:
                logger.debug("Progress message delete before question (best-effort): %s", e)
            status_msg_ts = None
        # 進捗履歴をファイルとして添付
        file_uploads = list(extra_files or [])
        if all_status_history:
            file_uploads.append({
                "content": "\n".join(all_status_history),
                "filename": "progress.txt",
                "title": t("status_history_title"),
            })
        posted = False
        if file_uploads:
            try:
                client.files_upload_v2(
                    channel=channel, thread_ts=thread_ts,
                    initial_comment=text,
                    file_uploads=file_uploads,
                )
                posted = True
            except Exception as e:
                logger.error("Question file upload error PID %d: %s", pid, e)
        if not posted:
            try:
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=text,
                )
            except Exception as e:
                logger.error("JSONL question post error PID %d: %s", pid, e)
        # pending_question を設定（最初の質問のみ対応）
        if metadata and metadata.get("questions"):
            q = metadata["questions"][0]
            pq = {
                "question": q.get("question", ""),
                "options": q["options"],
                "multi_select": q["multi_select"],
                "is_plan_approval": q.get("is_plan_approval", False),
            }
            inst["pending_question"] = pq
            # Sessionにも保存（プロセス終了後の --resume 用）
            session_ref = inst.get("session")
            if session_ref:
                session_ref.pending_question = pq

    def _post_plan_approval(question_text: str, metadata: dict | None):
        """プラン承認質問を投稿。plan_contentがあればファイルとして添付。"""
        plan_content = metadata.get("plan_content", "") if metadata else ""
        extra_files: list[dict] = []
        if plan_content:
            extra_files.append({
                "content": plan_content,
                "filename": "plan.md",
                "title": t("question_plan_approval_required"),
            })
        # プラン承認マーカーをmetadataに追加（_post_question→pending_questionに伝播）
        if metadata and metadata.get("questions"):
            metadata["questions"][0]["is_plan_approval"] = True
        _post_question(question_text, metadata, extra_files=extra_files)

    # 外部テイクオーバー検出用タイマー（bridge起動タスクのみ）
    _last_takeover_check: float = time.time()
    _TAKEOVER_CHECK_INTERVAL = 15.0  # 秒

    while _is_process_alive(pid):
        watcher.wait(timeout=_PERIODIC_CHECK_INTERVAL)

        # 外部テイクオーバー検出（bridge起動タスクのみ、定期チェック）
        now = time.time()
        if task_ref and task_ref.process and inst.get("bind_mode") != "live":
            if (now - _last_takeover_check) >= _TAKEOVER_CHECK_INTERVAL:
                _last_takeover_check = now
                session_ref = inst.get("session")
                if session_ref:
                    external = _detect_external_takeover(inst, session_ref)
                    if external:
                        _handle_external_takeover(inst, external, session_ref,
                                                  thread_ts, channel, client)
                        # inst が更新されたので pid を差し替えて監視を継続
                        old_jsonl = jsonl_path
                        pid = inst["pid"]
                        jsonl_path = inst.get("jsonl_path", jsonl_path)
                        fixed_jsonl = inst.get("fixed_jsonl", False)
                        skip_exit = inst.get("skip_exit_message", False)
                        display_prefix = inst.get("display_prefix", display_prefix)
                        # _monitored_jsonl_paths を更新（旧パス除去・新パス追加）
                        # これがないと _find_session_jsonl が別セッションのJSONLを拾ってしまう
                        if old_jsonl and old_jsonl != jsonl_path:
                            _monitored_jsonl_paths.discard(old_jsonl)
                        if jsonl_path:
                            _monitored_jsonl_paths.add(jsonl_path)
                            watcher.add_path(jsonl_path)
                        # JSONL オフセットをリセット（既存内容はスキップ）
                        try:
                            file_offset = os.path.getsize(jsonl_path)
                        except OSError:
                            pass
                        _finalize_progress()
                        status_msg_ts = None
                        status_lines = []
                        latest_text = None
                        continue

        # 新しい入力があればステータス行をリセット（同じ進捗メッセージを使い続ける）
        if "input_msg_ts" in inst:
            _finalize_progress()
            inst.pop("input_msg_ts")
            inst.pop("input_msg_text", "")
            status_lines = []
            latest_text = None

        # JONLファイルが変わった可能性をチェック（外部インスタンス用）
        if not fixed_jsonl:
            cwd = inst.get("cwd", "")
            new_path = _find_session_jsonl(cwd, exclude_paths=_monitored_jsonl_paths)
            if new_path and new_path != jsonl_path:
                _finalize_progress()
                jsonl_path = new_path
                inst["jsonl_path"] = jsonl_path
                watcher.add_path(jsonl_path)
                file_offset = 0

        # jsonl_pathがまだ見つかっていない場合はスキップ（bind時にJSONL未発見でも起動可能）
        if not jsonl_path:
            continue

        new_entries, file_offset = _read_new_jsonl_entries(jsonl_path, file_offset)
        if not new_entries:
            # バッファリング中のプラン承認のタイムアウトチェック
            if pending_plan_approval:
                if (time.time() - pending_plan_approval[2]) >= 15.0:
                    _finalize_progress()
                    _post_plan_approval(pending_plan_approval[0], pending_plan_approval[1])
                    pending_plan_approval = None
                    status_msg_ts = None
                    status_lines = []
                    latest_text = None

            # サブエージェントJSONLのみ活動がある場合もチェック
            if subagents_dir:
                sub_entries = _read_subagent_jsonl_entries(subagents_dir, subagent_offsets)
                if sub_entries:
                    has_sub_status = False
                    for agent_label, entry in sub_entries:
                        for category, text, metadata in _classify_jsonl_entry(entry):
                            if category == "status":
                                status_lines.append(f"  ↳ {text}")
                                all_status_history.append(f"  ↳ {text}")
                                has_sub_status = True
                    if has_sub_status:
                        last_jsonl_update = time.time()
                        if task_ref:
                            task_ref.last_activity = last_jsonl_update
                        _flush_progress()
                    continue

            # ハートビート: JSONL更新がない間も定期的に進捗メッセージを更新
            # （最終更新時刻の経過表示を更新するため）
            now = time.time()
            if status_msg_ts and (now - _last_heartbeat) >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = now
                _flush_progress()

            continue

        # エントリを分類して処理
        last_jsonl_update = time.time()
        if task_ref:
            task_ref.last_activity = last_jsonl_update
        text_parts: list[str] = []
        has_status = False

        for entry in new_entries:
            _extract_task_info(entry)
            for category, text, metadata in _classify_jsonl_entry(entry):
                if category == "status":
                    # thinkingが来たら新しいセットを開始（最新セットのみ表示）
                    if text.startswith(":thought_balloon:"):
                        status_lines = []
                    status_lines.append(text)
                    all_status_history.append(text)
                    has_status = True
                elif category == "text":
                    text_parts.append(text)
                elif category == "question":
                    # 先にテキストを更新
                    if text_parts:
                        _update_text("\n".join(text_parts))
                        text_parts = []
                    _finalize_progress()
                    # プラン承認はバッファリング（概要テキストを先に表示するため）
                    if metadata and metadata.get("plan_content") is not None:
                        pending_plan_approval = [text, metadata, time.time()]
                    else:
                        _post_question(text, metadata)
                    # ステータス内容をリセット（進捗メッセージは同一メッセージを使い続ける）
                    status_lines = []
                    latest_text = None
                    has_status = False

        # サブエージェントJSONL監視
        if subagents_dir:
            sub_entries = _read_subagent_jsonl_entries(subagents_dir, subagent_offsets)
            for agent_label, entry in sub_entries:
                for category, text, metadata in _classify_jsonl_entry(entry):
                    if category == "status":
                        status_lines.append(f"  ↳ {text}")
                        all_status_history.append(f"  ↳ {text}")
                        has_status = True

        # バッファリング中のプラン承認: テキストが来たら概要→プラン→承認の順で投稿
        if pending_plan_approval:
            if text_parts:
                _update_text("\n".join(text_parts))
                text_parts = []
                _finalize_progress()
                _post_plan_approval(pending_plan_approval[0], pending_plan_approval[1])
                pending_plan_approval = None
                status_lines = []
                latest_text = None
            else:
                # タイムアウト: テキストが来なくても投稿（約15秒）
                if (time.time() - pending_plan_approval[2]) >= 15.0:
                    _finalize_progress()
                    _post_plan_approval(pending_plan_approval[0], pending_plan_approval[1])
                    pending_plan_approval = None
                    status_lines = []
                    latest_text = None

        # 全エントリ処理後にまとめて更新
        if text_parts:
            _update_text("\n".join(text_parts))
        elif has_status:
            _flush_progress()

    # プロセス終了 → 残りのエントリを処理
    new_entries, file_offset = _read_new_jsonl_entries(jsonl_path, file_offset)
    if new_entries:
        text_parts = []
        has_status = False
        for entry in new_entries:
            _extract_task_info(entry)
            for category, text, metadata in _classify_jsonl_entry(entry):
                if category == "status":
                    # thinkingが来たら新しいセットを開始（最新セットのみ表示）
                    if text.startswith(":thought_balloon:"):
                        status_lines = []
                    status_lines.append(text)
                    all_status_history.append(text)
                    has_status = True
                elif category == "text":
                    text_parts.append(text)
                elif category == "question":
                    if text_parts:
                        _update_text("\n".join(text_parts))
                        text_parts = []
                    _finalize_progress()
                    if metadata and metadata.get("plan_content") is not None:
                        pending_plan_approval = [text, metadata, time.time()]
                    else:
                        _post_question(text, metadata)
                    status_msg_ts = None
                    status_lines = []
                    latest_text = None
                    has_status = False
        # サブエージェントJSONL残りエントリ処理
        if subagents_dir:
            sub_entries = _read_subagent_jsonl_entries(subagents_dir, subagent_offsets)
            for agent_label, entry in sub_entries:
                for category, text, metadata in _classify_jsonl_entry(entry):
                    if category == "status":
                        status_lines.append(f"  ↳ {text}")
                        all_status_history.append(f"  ↳ {text}")
                        has_status = True
        # バッファリング中のプラン承認をフラッシュ
        if pending_plan_approval:
            if text_parts:
                _update_text("\n".join(text_parts))
                text_parts = []
            _finalize_progress()
            _post_plan_approval(pending_plan_approval[0], pending_plan_approval[1])
            pending_plan_approval = None
            status_msg_ts = None
            status_lines = []
            latest_text = None
        elif text_parts:
            _update_text("\n".join(text_parts))
        elif has_status:
            _flush_progress()

    # バッファリング中のプラン承認が残っていたらフラッシュ
    if pending_plan_approval:
        _finalize_progress()
        _post_plan_approval(pending_plan_approval[0], pending_plan_approval[1])
        pending_plan_approval = None

    # 残った進捗を確定
    _finalize_progress()

    # watchdog 停止
    watcher.stop()
    inst.pop("_watcher", None)

    # ステータス履歴と進捗メッセージtsをinstに格納（_executeで使用）
    inst["_status_history"] = all_status_history
    inst["_status_msg_ts"] = status_msg_ts
    logger.debug("_monitor_session_jsonl: finished, history_len=%d status_msg_ts=%s thread=%s",
                 len(all_status_history), status_msg_ts, thread_ts)

    # プロセス終了通知（外部インスタンス用）
    if not skip_exit:
        # 外部テイクオーバーで引き継がれたプロセスの終了は専用メッセージ
        exit_key = "external_takeover_ended" if inst.get("external_takeover") else "session_pid_exited"
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=t(exit_key, pid=pid),
            )
        except Exception:
            pass

    # フォーク用クリーンアップ（bridge-spawned タスクは _execute の finally で既に除去済み）
    if inst.get("session") and not inst.get("skip_exit_message"):
        for ts, data in list(instance_threads.items()):
            if data is inst:
                instance_threads.pop(ts, None)
                break


def _extract_session_info_from_jsonl(task: Task, session: Optional["Session"], jsonl_path: str):
    """JONLファイルからsession_idとresultを抽出（フォールバック用）。
    session が指定されていれば claude_session_id も更新する。"""
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry_type = entry.get("type", "")
                msg = entry.get("message", {})
                # sessionId
                if isinstance(msg, dict):
                    sid = msg.get("sessionId") or entry.get("sessionId")
                else:
                    sid = entry.get("sessionId")
                if sid:
                    if session:
                        session.claude_session_id = sid
                        runner.save_sessions()
                # result
                if entry_type == "result":
                    result_data = entry.get("result", {})
                    result_text = ""
                    if isinstance(result_data, dict):
                        for content in result_data.get("content", []):
                            if isinstance(content, dict) and content.get("type") == "text":
                                result_text += content["text"]
                    elif isinstance(result_data, str):
                        result_text = result_data
                    if result_text:
                        task.result = result_text
                # text from assistant (最後のtext応答をresultとして使用)
                if entry_type == "assistant" and isinstance(msg, dict):
                    for c in msg.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "").strip()
                            if text:
                                task.result = text
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            task.tool_calls.append({
                                "name": c.get("name", "?"),
                                "input": _summarize_input(c.get("input", {})) if isinstance(c.get("input"), dict) else str(c.get("input", ""))[:80],
                            })
    except OSError:
        pass


def _strip_ansi(text: str) -> str:
    """ANSIエスケープシーケンスを除去"""
    # CSI sequences: ESC [ (parameter/intermediate bytes 0x20-0x3F)* (final byte 0x40-0x7E)
    # 標準シーケンス(\x1b[0m等)とプライベートモード(\x1b[?25h, \x1b[<u等)を両方カバー
    text = re.sub(r'\x1b\[[\x20-\x3f]*[\x40-\x7e]', '', text)
    # OSC sequences: ESC ] ... (BEL or ST)
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    # Character set selection: ESC ( / ESC )
    text = re.sub(r'\x1b[()][A-Z0-9]', '', text)
    # 残りのESC文字を除去（未知のエスケープシーケンス対策）
    text = text.replace('\x1b', '')
    return text


def _set_pty_size(fd: int, cols: int = 120, rows: int = 40):
    """疑似ターミナルのウィンドウサイズを設定"""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _detect_permission_prompt(text: str) -> dict | None:
    """テキストから番号付き選択肢パターン（許可プロンプト等）を検出。
    検出時: {"description": str, "options": [{"label": str}, ...]} を返す。
    未検出時: None
    複数行にまたがる選択肢テキスト（行折り返し）にも対応。"""
    lines = text.strip().split("\n")

    # 番号付き選択肢を探す（例: "1. Yes", "  2. No"）
    option_lines = []
    option_start_idx = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if option_lines:
                break  # 空行で選択肢終了
            continue

        m = re.match(r'^[>❯\s]*(\d+)\.\s+(.+)$', stripped)
        if m:
            num = int(m.group(1))
            label = m.group(2).strip()
            if num == len(option_lines) + 1:  # 連番チェック
                if not option_lines:
                    option_start_idx = i
                option_lines.append({"label": label})
            elif option_lines:
                break  # 連番が崩れたので選択肢終了
        elif option_lines:
            # 番号なし非空行 → 前の選択肢の折り返し継続行
            if re.match(r'^[❯>]', stripped):
                break  # プロンプト文字は選択肢終了
            option_lines[-1]["label"] += " " + stripped

    if len(option_lines) < 2:
        return None

    # 選択肢より前の行から説明文を抽出
    desc_lines = []
    if option_start_idx > 0:
        for i in range(max(0, option_start_idx - 5), option_start_idx):
            line = lines[i].strip()
            if line:
                desc_lines.append(line)

    description = "\n".join(desc_lines) if desc_lines else t("question_input_required")

    return {
        "description": description,
        "options": option_lines,
    }


def _post_permission_prompt(prompt_info: dict, thread_ts: str, channel_id: str,
                            client: WebClient, inst: dict):
    """許可プロンプトをSlackに投稿し、pending_questionを設定"""
    description = prompt_info["description"]
    options = prompt_info["options"]

    lines = [t("question_cli_header"), f"{description}"]
    for i, opt in enumerate(options, 1):
        lines.append(f"  {i}. {opt['label']}")
    lines.append(t("question_reply_with_number"))

    text = "\n".join(lines)
    try:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
    except Exception as e:
        logger.error("Permission prompt post error: %s", e)

    inst["pending_question"] = {
        "options": options,
        "multi_select": False,
    }


# ── Claude Code ランナー ──────────────────────────────────
class ClaudeCodeRunner:
    """Project → Session → Task の3層構造でClaude Codeタスクを管理"""

    def __init__(self, slack_client: WebClient):
        self.client = slack_client
        self.projects: dict[str, Project] = {}  # channel_id → Project
        self.lock = threading.Lock()
        self._task_counter = 0
        self.directory_history: dict[str, list[str]] = {}  # channel_id → [dir_path, ...]
        # 進行中タスクのライブスナップショット（中断検出用）
        self._interrupted_lock = threading.Lock()
        self._active_tasks_snapshot: list[dict] = []
        # App Home ダッシュボード用: 終了済みセッションの薄い履歴
        self._session_history_lock = threading.Lock()
        self._session_history: list[dict] = self.load_session_history()

    def _next_id(self) -> int:
        self._task_counter += 1
        return self._task_counter

    # ── プロジェクト管理 ──
    def get_or_create_project(self, channel_id: str) -> Project:
        """チャンネルのProjectを取得、なければ作成"""
        if channel_id not in self.projects:
            roots = self.load_channel_roots()
            root_dir = roots.get(channel_id)
            # プロジェクト単位の承認済みツールを復元
            saved_tools = self.load_project_tools()
            approved = set(saved_tools.get(channel_id, []))
            project = Project(channel_id=channel_id, root_dir=root_dir, approved_tools=approved)
            # 永続化セッションを復元
            saved = self.load_sessions()
            if channel_id in saved:
                logger.info("get_or_create_project: restoring %d sessions for channel=%s",
                            len(saved[channel_id]), channel_id)
                self._restore_sessions(project, saved[channel_id])
                logger.info("get_or_create_project: after restore, project has %d sessions: %s",
                            len(project.sessions), list(project.sessions.keys()))
            else:
                logger.info("get_or_create_project: no saved sessions for channel=%s", channel_id)
            self.projects[channel_id] = project
        return self.projects[channel_id]

    def get_project(self, channel_id: str) -> Optional[Project]:
        return self.projects.get(channel_id)

    # ── ディレクトリ履歴 ──
    def record_directory(self, channel_id: str, dir_path: str):
        """チャンネルのディレクトリ使用履歴を記録"""
        history = self.directory_history.setdefault(channel_id, [])
        # 既存エントリを先頭に移動
        if dir_path in history:
            history.remove(dir_path)
        history.insert(0, dir_path)
        # 上限を超えたら古いものを削除
        if len(history) > DIRECTORY_HISTORY_MAX:
            del history[DIRECTORY_HISTORY_MAX:]
        self.save_directory_history()

    def save_directory_history(self):
        """ディレクトリ履歴を永続化"""
        try:
            with open(DIRECTORY_HISTORY_FILE, "w") as f:
                json.dump(self.directory_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save directory history: %s", e)

    def load_directory_history(self):
        """ディレクトリ履歴を読み込み"""
        try:
            with open(DIRECTORY_HISTORY_FILE, "r") as f:
                self.directory_history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            logger.warning("Failed to load directory history: %s", e)

    # ── チャンネルルートディレクトリ ──
    def save_channel_roots(self):
        """チャンネルルート設定を永続化（未ロードチャンネルの既存データを保持）"""
        # 既存のファイルデータを読み込み（未ロードチャンネルのルート保持）
        roots = self.load_channel_roots()
        # メモリ上のプロジェクトでデータを上書き
        for channel_id, project in self.projects.items():
            if project.root_dir:
                roots[channel_id] = project.root_dir
            else:
                roots.pop(channel_id, None)
        try:
            with open(CHANNEL_ROOTS_FILE, "w") as f:
                json.dump(roots, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save channel roots: %s", e)

    def load_channel_roots(self) -> dict[str, str]:
        """チャンネルルート設定を読み込み"""
        try:
            with open(CHANNEL_ROOTS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except Exception as e:
            logger.warning("Failed to load channel roots: %s", e)
            return {}

    def save_project_tools(self):
        """プロジェクト単位の承認済みツールを永続化"""
        data = self.load_project_tools()
        for channel_id, project in self.projects.items():
            if project.approved_tools:
                data[channel_id] = sorted(project.approved_tools)
            else:
                data.pop(channel_id, None)
        try:
            with open(PROJECT_TOOLS_FILE, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save project tools: %s", e)

    def load_project_tools(self) -> dict[str, list[str]]:
        """プロジェクト単位の承認済みツールを読み込み"""
        try:
            with open(PROJECT_TOOLS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except Exception as e:
            logger.warning("Failed to load project tools: %s", e)
            return {}

    def save_sessions(self):
        """セッション情報を永続化（未ロードチャンネルの既存データを保持）"""
        now = datetime.now()
        cutoff = now - timedelta(days=SESSIONS_MAX_AGE_DAYS)
        # 既存のファイルデータを読み込み（未ロードチャンネルのセッション保持）
        data = self.load_sessions()
        # メモリ上のプロジェクトでデータを上書き
        for channel_id, project in self.projects.items():
            channel_sessions: dict[str, dict] = {}
            for thread_ts, session in project.sessions.items():
                if not session.claude_session_id:
                    continue
                created = session.created_at or now
                if created < cutoff:
                    continue
                channel_sessions[thread_ts] = {
                    "thread_ts": session.thread_ts,
                    "channel_id": session.channel_id,
                    "claude_session_id": session.claude_session_id,
                    "label_emoji": session.label_emoji,
                    "label_name": session.label_name,
                    "working_dir": session.working_dir,
                    "created_at": created.isoformat(),
                    "prompts": session.prompts,
                }
            # チャンネルあたり上限を適用（古い順に削除）
            if len(channel_sessions) > SESSIONS_MAX_PER_CHANNEL:
                sorted_items = sorted(
                    channel_sessions.items(),
                    key=lambda x: x[1].get("created_at", ""),
                )
                channel_sessions = dict(sorted_items[-SESSIONS_MAX_PER_CHANNEL:])
            if channel_sessions:
                data[channel_id] = channel_sessions
            else:
                data.pop(channel_id, None)
        # 未ロードチャンネルの古いセッションも期限切れ削除
        for channel_id in list(data.keys()):
            if channel_id in self.projects:
                continue  # 上で処理済み
            filtered = {
                ts: s for ts, s in data[channel_id].items()
                if s.get("created_at", "") >= cutoff.isoformat()
            }
            if filtered:
                data[channel_id] = filtered
            else:
                del data[channel_id]
        try:
            dir_name = os.path.dirname(SESSIONS_FILE)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, SESSIONS_FILE)
            except BaseException:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.warning("Failed to save sessions: %s", e)

    # ── 中断タスクトラッキング（プロセス突然死検出用） ──
    def _write_interrupted_file_locked(self):
        """interrupted_tasks.json を atomic に書き出す（_interrupted_lock 取得済み前提）"""
        try:
            if not self._active_tasks_snapshot:
                try:
                    os.remove(INTERRUPTED_TASKS_FILE)
                except FileNotFoundError:
                    pass
                return
            tmp = INTERRUPTED_TASKS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(
                    {"tasks": self._active_tasks_snapshot},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp, INTERRUPTED_TASKS_FILE)
        except Exception as e:
            logger.warning("Failed to write interrupted_tasks.json: %s", e)

    def record_active_task(self, session: "Session", task: "Task"):
        """タスク開始時にスナップショットを更新。1スレッド1エントリ。"""
        entry = {
            "channel_id": session.channel_id,
            "thread_ts": session.thread_ts,
            "label_emoji": session.label_emoji,
            "label_name": session.label_name,
            "claude_session_id": session.claude_session_id or task.resume_session,
            "working_dir": session.working_dir,
            "user_id": task.user_id,
            "prompt": (task.prompt or "")[:500],
            "started_at": task.started_at.isoformat() if task.started_at else None,
        }
        key = (session.channel_id, session.thread_ts)
        with self._interrupted_lock:
            self._active_tasks_snapshot = [
                e for e in self._active_tasks_snapshot
                if (e["channel_id"], e["thread_ts"]) != key
            ]
            self._active_tasks_snapshot.append(entry)
            self._write_interrupted_file_locked()

    def clear_active_task(self, session: "Session"):
        """タスク終了時にスナップショットからエントリを除去。"""
        key = (session.channel_id, session.thread_ts)
        with self._interrupted_lock:
            before = len(self._active_tasks_snapshot)
            self._active_tasks_snapshot = [
                e for e in self._active_tasks_snapshot
                if (e["channel_id"], e["thread_ts"]) != key
            ]
            if len(self._active_tasks_snapshot) != before:
                self._write_interrupted_file_locked()

    def load_and_clear_interrupted_tasks(self) -> list[dict]:
        """起動時呼び出し: ファイルを読んで返し、即削除する。"""
        with self._interrupted_lock:
            tasks: list[dict] = []
            try:
                with open(INTERRUPTED_TASKS_FILE, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    tasks = data.get("tasks") or []
            except FileNotFoundError:
                pass
            except json.JSONDecodeError:
                logger.warning("interrupted_tasks.json corrupted, treating as empty")
            except Exception as e:
                logger.warning("Failed to load interrupted_tasks.json: %s", e)
            try:
                os.remove(INTERRUPTED_TASKS_FILE)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning("Failed to remove interrupted_tasks.json: %s", e)
            self._active_tasks_snapshot = []
            return tasks

    def load_sessions(self) -> dict[str, dict[str, dict]]:
        """永続化セッション情報を読み込み"""
        try:
            with open(SESSIONS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except Exception as e:
            logger.warning("Failed to load sessions: %s", e)
            return {}

    # ── App Home ダッシュボード用: 終了済みセッション履歴 ──
    def load_session_history(self) -> list[dict]:
        """session_history.json を読み込み（薄い終了済みセッション記録のリスト）"""
        try:
            with open(SESSION_HISTORY_FILE, "r") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        except Exception as e:
            logger.warning("Failed to load session history: %s", e)
            return []

    def _save_session_history_locked(self):
        """_session_history_lock 取得済み前提で atomic 書き出し。"""
        try:
            dir_name = os.path.dirname(SESSION_HISTORY_FILE)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._session_history, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, SESSION_HISTORY_FILE)
            except BaseException:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.warning("Failed to save session history: %s", e)

    def record_session_history(self, session: "Session", task: "Task"):
        """セッションの最新タスク終了時に薄い履歴レコードを追記。
        同一スレッドの既存レコードは置き換える（セッション=最新タスクのみ保持）。"""
        status = task.status.value if task.status else "unknown"
        entry = {
            "channel_id": session.channel_id,
            "thread_ts": session.thread_ts,
            "latest_msg_ts": _task_anchor_ts(task),
            "session_id": session.claude_session_id,
            "label_emoji": session.label_emoji,
            "label_name": session.label_name,
            "working_dir": session.working_dir,
            "user_id": task.user_id,
            "latest_status": status,
            "latest_prompt": (task.prompt or "")[:200],
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "tool_count": len(task.tool_calls),
        }
        key = (session.channel_id, session.thread_ts)
        with self._session_history_lock:
            self._session_history = [
                e for e in self._session_history
                if (e.get("channel_id"), e.get("thread_ts")) != key
            ]
            self._session_history.append(entry)
            # 全体上限（末尾が最新なので先頭=古いものを削る）
            if len(self._session_history) > SESSION_HISTORY_MAX:
                self._session_history = self._session_history[-SESSION_HISTORY_MAX:]
            self._save_session_history_locked()

    def get_session_history(self) -> list[dict]:
        """終了済みセッション履歴のコピーを新しい順で返す。"""
        with self._session_history_lock:
            return list(reversed(self._session_history))

    @staticmethod
    def _migrate_prompts(sdata: dict) -> list[str]:
        """旧形式 (first_prompt/latest_prompt) から prompts リストへマイグレーション"""
        prompts = []
        fp = sdata.get("first_prompt")
        lp = sdata.get("latest_prompt")
        if fp:
            prompts.append(fp)
        if lp and lp != fp:
            prompts.append(lp)
        return prompts

    def _restore_sessions(self, project: Project, channel_data: dict[str, dict]):
        """永続化データからSessionオブジェクトをProjectに復元"""
        now = datetime.now()
        cutoff = now - timedelta(days=SESSIONS_MAX_AGE_DAYS)
        restored_count = 0
        skipped_reasons: dict[str, list[str]] = {"in_memory": [], "expired": []}
        for thread_ts, sdata in channel_data.items():
            # 既にメモリにあるセッションは上書きしない
            if thread_ts in project.sessions:
                skipped_reasons["in_memory"].append(thread_ts)
                continue
            # 古いセッションは復元スキップ
            created_at_str = sdata.get("created_at")
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                except (ValueError, TypeError):
                    created_at = now
                if created_at < cutoff:
                    skipped_reasons["expired"].append(thread_ts)
                    continue
            else:
                created_at = now
            session = Session(
                thread_ts=thread_ts,
                channel_id=sdata.get("channel_id", project.channel_id),
                claude_session_id=sdata.get("claude_session_id"),
                label_emoji=sdata.get("label_emoji", ""),
                label_name=sdata.get("label_name", ""),
                working_dir=sdata.get("working_dir"),
                created_at=created_at,
                prompts=sdata.get("prompts") or self._migrate_prompts(sdata),
            )
            project.sessions[thread_ts] = session
            restored_count += 1
        if skipped_reasons["in_memory"] or skipped_reasons["expired"]:
            logger.info("_restore_sessions: restored=%d skipped_in_memory=%d skipped_expired=%d channel=%s",
                        restored_count, len(skipped_reasons["in_memory"]), len(skipped_reasons["expired"]),
                        project.channel_id)

    def get_channel_root(self, channel_id: str) -> Optional[str]:
        """チャンネルのルートディレクトリを取得"""
        project = self.projects.get(channel_id)
        if project and project.root_dir:
            return project.root_dir
        roots = self.load_channel_roots()
        return roots.get(channel_id)

    # ── タスク検索 ──
    def find_task_globally(self, task_id: int) -> Optional[tuple[Project, Session, Task]]:
        """全プロジェクトからタスクIDで検索"""
        for project in self.projects.values():
            result = project.find_task_by_id(task_id)
            if result:
                session, task = result
                return (project, session, task)
        return None

    # ── コマンドビルド ──
    def build_command(self, task: Task, prompt_as_arg: bool = False,
                      session: Optional[Session] = None,
                      project: Optional[Project] = None) -> list[str]:
        cmd = [CLAUDE_CMD, "-p"]
        cmd.append("--verbose")

        tools = task.allowed_tools or DEFAULT_ALLOWED_TOOLS
        # Session/Project 単位の承認済みツールをマージ
        extra_tools: set[str] = set()
        if session and session.approved_tools:
            extra_tools |= session.approved_tools
        if project and project.approved_tools:
            extra_tools |= project.approved_tools
        if extra_tools:
            base_set = set(t_.strip() for t_ in tools.split(",")) if tools else set()
            merged = base_set | extra_tools
            tools = ",".join(sorted(merged))
        if tools:
            cmd.extend(["--allowedTools", tools])

        # disallowedTools: タスクに明示指定があればそれを使用、なければデフォルト。
        # デフォルトではAskUserQuestion（-pモードで自動回答されるため）と
        # ExitPlanMode（プラン承認フローのため）を無効化。
        # プラン承認後はtask.disallowed_tools="AskUserQuestion"が設定され、
        # ExitPlanModeが許可される。
        disallowed = task.disallowed_tools if task.disallowed_tools is not None else "AskUserQuestion,ExitPlanMode"
        if disallowed:
            cmd.extend(["--disallowedTools", disallowed])
        system_prompt = t("prompt_system_append")
        if tools:
            system_prompt += t("prompt_allowed_tools_info", tools=tools)
        cmd.extend(["--append-system-prompt", system_prompt])

        if task.resume_session:
            cmd.extend(["--resume", task.resume_session])
        if task.fork_session:
            cmd.append("--fork-session")

        if prompt_as_arg:
            cmd.append("--")
            cmd.append(task.prompt)

        return cmd

    # ── タスク実行 ──
    def run_task(self, project: Project, session: Session, task: Task) -> Optional[str]:
        """タスク実行を開始。エラー時はメッセージ文字列を返す"""
        with self.lock:
            if task.id == 0:
                task.id = self._next_id()
            session.tasks.append(task)
            # プロンプト履歴を更新（永続化用）
            if task.prompt:
                session.prompts.append(task.prompt)
            self.save_sessions()

        thread = threading.Thread(
            target=self._execute, args=(project, session, task), daemon=True
        )
        thread.start()
        # App Home ダッシュボード: 開始を即時反映
        _schedule_home_refresh()
        return None

    def _fire_one_followup(self, project: "Project", session: "Session"):
        """pending_followup を1件取り出し --resume 新タスクとして発火（FIFO・直列）。
        タスク完了/中断時の _execute finally から呼ばれる。残りキューは発火した
        タスク自身の finally が順に処理するため、ここでは1件だけ起こせばよい。
        この時点で _execute は JSONL を読み切り、session.claude_session_id は確定済み
        （取得できなければ resume 先が存在しないので新規タスクとして起動）。"""
        with self.lock:
            # ツール許可リクエスト待ち中はキューを発火しない。
            # タスクが [TOOL_REQUEST:...] を返して終了した場合、ユーザーが
            # Slack のボタンで応答するまで保留する（承認/却下＋コメント →
            # その再実行タスクの finally がキューを処理、却下のみ → 終了するので
            # _handle_tool_request_action からここを呼び直してキューを処理）。
            if session.pending_tool_request is not None:
                logger.info("_fire_one_followup: deferred (pending_tool_request) thread=%s",
                            session.thread_ts)
                return
            if not session.pending_followup:
                return
            fu = session.pending_followup.pop(0)
        text = fu.get("text", "")
        if not text or not text.strip():
            return
        instruction_preview = text.strip().replace("\n", " ")[:80]  # 補足前置前の生の指示（表示用）
        # cancel モード（@bot cancel <指示>）: 中断した旨の補足を前置して文脈を補う
        if fu.get("mode") == "cancel":
            text = t("followup_cancel_prefix") + text
        # 添付ファイル（queue パスでは DL 済みパスが text に埋め込まれているため通常は空）
        files = fu.get("files") or []
        if files and session.working_dir:
            file_paths = _download_slack_files(files, session.working_dir)
            if file_paths:
                text = _augment_prompt_with_files(text, file_paths)

        task = Task(
            id=0,
            prompt=text,
            allowed_tools=session.consume_tools(),
            user_id=fu.get("user_id", ""),
        )
        if session.claude_session_id:
            task.resume_session = session.claude_session_id
            if session.consume_fork():
                task.fork_session = True
            logger.info("_fire_one_followup: firing with --resume sid=%s mode=%s thread=%s",
                        session.claude_session_id[:16], fu.get("mode"), session.thread_ts)
        else:
            logger.warning("_fire_one_followup: no session_id, firing fresh task thread=%s", session.thread_ts)

        self.assign_task_id(task)
        task.reaction_targets = _build_reaction_targets(
            session.channel_id, fu.get("request_msg_ts"), session.thread_ts,
        )
        self._swap_task_reaction(task, REACTION_RECEIVED)
        self._post_to_session(session, t("followup_firing", instruction=instruction_preview))
        err = self.run_task(project, session, task)
        if err:
            self._post_to_session(session, err)

    def _execute(self, project: Project, session: Session, task: Task):
        logger.info("_execute: starting task=%s thread=%s resume=%s cwd=%s",
                    task.short_id, session.thread_ts, task.resume_session[:16] if task.resume_session else "None", session.working_dir)
        cwd = session.working_dir
        if not cwd:
            # Safety net（新フローでは到達しないはず）
            task.status = TaskStatus.FAILED
            task.error = t("task_working_dir_not_set")
            task.completed_at = datetime.now()
            self._post_to_session(session, f"{session.label_emoji} {task.short_id}  :x: {t('task_working_dir_not_set')}")
            self._swap_task_reaction(task, REACTION_FAILED)
            return
        channel_id = session.channel_id
        thread_ts = session.thread_ts
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        # 進行中タスクスナップショットに記録（プロセス突然死検出用）
        self.record_active_task(session, task)

        display_label = f"{session.label_emoji} {task.short_id}"
        is_resume = task.resume_session is not None

        # PTYモード判定: プロンプトが200KB以下ならCLI引数で渡しPTYを使用
        prompt_bytes = task.prompt.encode("utf-8")
        use_pty = len(prompt_bytes) <= 200 * 1024
        master_fd = None
        registered_thread_ts = None
        jsonl_path = None
        inst = None

        try:
            # サブプロセス起動前のタイムスタンプを記録（JSONL検出用）
            start_time = time.time()
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"} | {"LANG": "en_US.UTF-8"}

            if use_pty:
                # PTYモード: 疑似ターミナルでサブプロセスを起動
                cmd = self.build_command(task, prompt_as_arg=True, session=session, project=project)
                master_fd, slave_fd = pty.openpty()
                _set_pty_size(master_fd, 120, 40)
                env["TERM"] = "xterm-256color"

                proc = subprocess.Popen(
                    cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
                os.close(slave_fd)
                task.process = proc
                task.master_fd = master_fd

                # PTYのmaster側を読み捨てるスレッドを起動
                # stdoutがPTY経由のため、読み取らないとカーネルバッファが
                # 溢れてCLIプロセスのwrite()がブロックしハングする
                def _drain_pty(fd):
                    try:
                        while True:
                            try:
                                data = os.read(fd, 4096)
                                if not data:
                                    break
                            except OSError:
                                break
                    except Exception:
                        pass
                threading.Thread(target=_drain_pty, args=(master_fd,),
                                 daemon=True).start()

                # instance_threadsに登録（スレッド返信ルーティング有効化）
                inst = {
                    "pid": proc.pid,
                    "cwd": cwd,
                    "master_fd": master_fd,
                    "task": task,
                    "session": session,
                    "display_prefix": display_label,
                    "skip_exit_message": True,
                }
                instance_threads[thread_ts] = inst
                registered_thread_ts = thread_ts

            else:
                # フォールバック: 従来のstdin方式（長大プロンプト用）
                cmd = self.build_command(task, session=session, project=project)
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=cwd,
                    env=env,
                )
                task.process = proc
                proc.stdin.write(task.prompt)
                proc.stdin.close()

            # 実行中リアクションへ差し替え
            self._swap_task_reaction(task, REACTION_RUNNING)

            # JONLファイルが現れるまでポーリング（プロセス終了まで探し続ける）
            # resumeタスクの場合、session_idからファイル名で直接特定する
            # （cwdマッチングでは同一ディレクトリの別セッションJSONLを掴むリスクがある）
            # 初回・forkタスクではsession_idが未知のためcwdマッチングを使用
            # fork-sessionは新しいセッションIDで新規JSONLを作成するためcwdマッチングを使用
            jsonl_path = None
            jsonl_is_existing = False  # resume時に既存ファイルが見つかった場合True
            is_fork = task.fork_session
            poll_i = 0
            while proc.poll() is None:
                if is_resume and task.resume_session and not is_fork:
                    # resume（非fork）: ファイル名（=session_id）で直接特定
                    found = _find_jsonl_path_for_session_id(task.resume_session)
                    if found:
                        jsonl_is_existing = True
                else:
                    # 初回・fork: cwdマッチング（新規ファイルのみ）
                    found = _find_session_jsonl(cwd, min_ctime=start_time, exclude_paths=_monitored_jsonl_paths)
                if found:
                    jsonl_path = found
                    _monitored_jsonl_paths.add(jsonl_path)
                    logger.debug("_execute: JSONL file found path=%s poll=%d existing=%s thread=%s", found, poll_i, jsonl_is_existing, thread_ts)
                    break
                poll_i += 1
                time.sleep(1)
            if not jsonl_path:
                logger.warning("_execute: JSONL file not found (process exited) pid=%d cwd=%s thread=%s", proc.pid, cwd, thread_ts)

            # JSONL監視（プロセス終了後でも残りエントリを処理してステータス履歴を取得する）
            jsonl_monitored = False
            if jsonl_path:
                # 既存ファイル（resume追記）: 既存内容をスキップして新規エントリのみ読む
                # 新規ファイル: 先頭から読む
                start_from_beginning = not jsonl_is_existing
                if proc.poll() is not None:
                    # プロセス既終了 — 全エントリを読み取るため先頭から開始
                    start_from_beginning = True
                    logger.debug("_execute: JSONL found but process already exited, reading from beginning pid=%d jsonl=%s thread=%s", proc.pid, jsonl_path, thread_ts)
                if registered_thread_ts and registered_thread_ts in instance_threads:
                    inst = instance_threads[registered_thread_ts]
                    inst["jsonl_path"] = jsonl_path
                    inst["fixed_jsonl"] = True
                    inst["start_from_beginning"] = start_from_beginning
                    inst["task"] = task
                    inst["session"] = session
                else:
                    inst = {
                        "pid": proc.pid,
                        "cwd": cwd,
                        "jsonl_path": jsonl_path,
                        "display_prefix": display_label,
                        "skip_exit_message": True,
                        "fixed_jsonl": True,
                        "start_from_beginning": start_from_beginning,
                        "task": task,
                        "session": session,
                    }
                _monitor_session_jsonl(inst, thread_ts, channel_id, self.client)
                jsonl_monitored = True

            proc.wait()

            # 外部テイクオーバーによる終了: 既にCOMPLETED設定済み
            if task.status == TaskStatus.COMPLETED and inst and inst.get("bind_mode") == "live":
                logger.info("_execute: task ended via external takeover, skipping completion flow thread=%s", thread_ts)
                self._swap_task_reaction(task, REACTION_COMPLETED)
                self._cleanup_status_message(inst, session, task)
                # テイクオーバー後のJSONLパスをクリーンアップ
                # （テイクオーバー時に _monitored_jsonl_paths に追加された新パス）
                takeover_jsonl = inst.get("jsonl_path")
                if takeover_jsonl:
                    _monitored_jsonl_paths.discard(takeover_jsonl)
                return

            if task.status == TaskStatus.CANCELLED:
                self._post_completion(session, task,
                                      f"{display_label}  {t('task_cancelled')}", inst)
                self._swap_task_reaction(task, REACTION_CANCELLED)
                self._cleanup_status_message(inst, session, task)
                return

            # JSONL からsession_idを取得できなかった場合のフォールバック
            if not session.claude_session_id:
                if is_resume and task.resume_session and not is_fork:
                    fallback_path = jsonl_path or _find_jsonl_path_for_session_id(task.resume_session)
                else:
                    fallback_path = jsonl_path or _find_session_jsonl(cwd, min_ctime=start_time, exclude_paths=_monitored_jsonl_paths)
                logger.debug("_execute: session_id not acquired, trying fallback fallback_path=%s jsonl_monitored=%s pid=%d thread=%s",
                             fallback_path, jsonl_monitored, proc.pid, thread_ts)
                if fallback_path:
                    _extract_session_info_from_jsonl(task, session, fallback_path)
                    if session.claude_session_id:
                        logger.debug("_execute: fallback session_id acquired sid=%s thread=%s",
                                     session.claude_session_id[:16], thread_ts)
                        self.save_sessions()
                    else:
                        logger.warning("_execute: fallback also failed to get session_id jsonl=%s thread=%s", fallback_path, thread_ts)
                else:
                    logger.warning("_execute: fallback JSONL file also not found pid=%d cwd=%s thread=%s", proc.pid, cwd, thread_ts)
            else:
                logger.debug("_execute: task completed session_id=%s thread=%s", session.claude_session_id[:16], thread_ts)

            logger.info("_execute: process finished pid=%d returncode=%d thread=%s",
                       proc.pid, proc.returncode, thread_ts)
            if proc.returncode == 0:
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now()
                elapsed = (task.completed_at - task.started_at).total_seconds()
                fallback_text, blocks, full_result = self._format_result(
                    task, session, elapsed, show_result=True
                )
                # 完了リアクション。_post_completion 内の _check_tool_request が
                # TOOL_REQUEST マーカーを検出した場合は内部で REACTION_AWAITING_TOOL に上書きする。
                self._swap_task_reaction(task, REACTION_COMPLETED)
                self._post_completion(session, task, fallback_text, inst,
                                      full_result=full_result, blocks=blocks)
            else:
                stderr_raw = proc.stderr.read() if proc.stderr else ""
                stderr_output = stderr_raw.decode("utf-8", errors="replace") if isinstance(stderr_raw, bytes) else stderr_raw
                task.status = TaskStatus.FAILED
                task.error = stderr_output
                task.completed_at = datetime.now()
                # エラーコンテキスト: 最後のツール呼び出しを表示
                error_parts = [f"{display_label}  {t('task_failed', code=proc.returncode)}"]
                if task.tool_calls:
                    last_tc = task.tool_calls[-1]
                    error_parts.append(t("task_last_action", tool=last_tc["name"], summary=last_tc.get("input", "")[:100]))
                error_parts.append(f"```{stderr_output[:1000]}```")
                self._swap_task_reaction(task, REACTION_FAILED)
                self._post_completion(
                    session, task,
                    "\n".join(error_parts),
                    inst,
                )

            # 進捗メッセージ削除
            self._cleanup_status_message(inst, session, task)

        except Exception as e:
            logger.error("_execute: exception in task thread=%s error=%s", thread_ts, e, exc_info=True)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.completed_at = datetime.now()
            error_parts = [f"{display_label}  {t('task_error', error=e)}"]
            if task.tool_calls:
                last_tc = task.tool_calls[-1]
                error_parts.append(t("task_last_action", tool=last_tc["name"], summary=last_tc.get("input", "")[:100]))
            self._swap_task_reaction(task, REACTION_FAILED)
            self._post_completion(session, task,
                                  "\n".join(error_parts), inst)
            self._cleanup_status_message(inst, session, task)

        finally:
            # テイクオーバー時はバインド監視スレッドが管理するため除去しない
            is_takeover = inst and inst.get("bind_mode") == "live"
            if jsonl_path and not is_takeover:
                _monitored_jsonl_paths.discard(jsonl_path)
            if master_fd is not None and not is_takeover:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                task.master_fd = None
            if registered_thread_ts and not is_takeover:
                instance_threads.pop(registered_thread_ts, None)
            # 進行中タスクスナップショットから除去
            # （プロセスがここまで生きていれば「中断」ではないので)
            self.clear_active_task(session)
            # App Home ダッシュボード: 終了済みセッション履歴を記録し即時更新
            try:
                self.record_session_history(session, task)
            except Exception as e:
                logger.warning("record_session_history failed: %s", e)
            _schedule_home_refresh()
            # 実行中に積まれた追加指示（pending_followup）を発火。
            # この時点で teardown 完了・session_id 確定済みなので競合なく --resume できる。
            # takeover（bind live）中は別フローが管理するため firing しない。
            if not is_takeover:
                try:
                    self._fire_one_followup(project, session)
                except Exception as e:
                    logger.warning("_fire_one_followup failed: %s", e)

    def _format_result(self, task: Task, session: Session, elapsed: float,
                       show_result: bool = True) -> tuple[str, list[dict], str | None]:
        """タスク完了メッセージを生成。(fallback_text, blocks, full_result_or_none) を返す。
        blocks: section/mrkdwn(ヘッダー) + markdown(本文) + section/mrkdwn(フッター) の構成。
        Claudeの応答本文は markdown ブロックで投稿し、表/リスト/コードが標準Markdownで描画される。
        full_result_or_none はメッセージに収まらない場合のみ非Noneでファイルアップロード用。"""
        cwd = session.working_dir or "(unknown)"
        dir_name = os.path.basename(cwd)
        display_label = f"{session.label_emoji} {task.short_id}"
        user_info = f"  <@{task.user_id}>" if task.user_id else ""
        header_lines = [f"{display_label}  {t('task_complete', elapsed=elapsed)}  :file_folder: `{dir_name}`{user_info}"]
        if task.tool_calls:
            tools_summary = ", ".join(f"`{tc['name']}`" for tc in task.tool_calls[:10])
            if len(task.tool_calls) > 10:
                tools_summary += t("task_tools_more", count=len(task.tool_calls) - 10)
            header_lines.append(t("task_tools_used", count=len(task.tool_calls), tools=tools_summary))
        # トークン使用量・コスト表示
        if task.input_tokens > 0 or task.output_tokens > 0:
            def _fmt_tokens(n: int) -> str:
                if n >= 1_000_000:
                    return f"{n / 1_000_000:.1f}M"
                if n >= 1_000:
                    return f"{n / 1_000:.1f}K"
                return str(n)
            token_line = t("task_tokens",
                           input=_fmt_tokens(task.input_tokens),
                           output=_fmt_tokens(task.output_tokens),
                           cache_read=_fmt_tokens(task.cache_read_tokens))
            # コスト概算（Sonnet 4 料金: input $3/M, output $15/M, cache_read $0.30/M, cache_creation $3.75/M）
            cost = (
                task.input_tokens * 3.0 / 1_000_000
                + task.output_tokens * 15.0 / 1_000_000
                + task.cache_read_tokens * 0.30 / 1_000_000
                + task.cache_creation_tokens * 3.75 / 1_000_000
            )
            token_line += "  " + t("task_cost_estimate", cost=cost)
            header_lines.append(token_line)
        header_text = "\n".join(header_lines)

        full_result = None
        result_md: str | None = None
        if show_result and task.result:
            # markdownブロック上限 12,000文字。margin入れて11,000を超えたらプレビュー + ファイル添付。
            if len(task.result) <= 11000:
                result_md = task.result
            else:
                result_md = task.result[:10000] + t("task_see_full_file")
                full_result = task.result

        footer_lines: list[str] = []
        if session.claude_session_id:
            footer_lines.append(f"_Session: `{session.claude_session_id[:12]}...`_")
            footer_lines.append(t("task_reply_to_continue"))
        footer_text = "\n".join(footer_lines)

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        ]
        if result_md:
            blocks.append({"type": "markdown", "text": result_md})
        if footer_text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": footer_text}})

        fallback_parts = [header_text]
        if result_md:
            fallback_parts.append(result_md[:300])
        if footer_text:
            fallback_parts.append(footer_text)
        fallback_text = "\n".join(fallback_parts)

        return fallback_text, blocks, full_result

    def assign_task_id(self, task: Task):
        """タスクIDを事前に割り当て（run_task前に呼び出し可能）"""
        with self.lock:
            if task.id == 0:
                task.id = self._next_id()

    def _swap_task_reaction(self, task: Task, new_reaction: str | None):
        """task.reaction_targets 全てに対して、現在のリアクションを外し new_reaction を付与。
        new_reaction=None なら除去のみ。ベストエフォート（失敗は debug ログのみ）。"""
        old = task.current_reaction
        if old == new_reaction:
            return
        for channel_id, msg_ts in task.reaction_targets:
            if old:
                try:
                    self.client.reactions_remove(
                        channel=channel_id, timestamp=msg_ts, name=old,
                    )
                except Exception as e:
                    logger.debug("reactions_remove (best-effort) %s on %s/%s: %s",
                                 old, channel_id, msg_ts, e)
            if new_reaction:
                try:
                    self.client.reactions_add(
                        channel=channel_id, timestamp=msg_ts, name=new_reaction,
                    )
                except Exception as e:
                    logger.debug("reactions_add (best-effort) %s on %s/%s: %s",
                                 new_reaction, channel_id, msg_ts, e)
        task.current_reaction = new_reaction

    def _post_to_session(self, session: Session, text: str):
        """セッション（スレッド）にメッセージを投稿"""
        try:
            if session.thread_ts:
                self.client.chat_postMessage(
                    channel=session.channel_id,
                    thread_ts=session.thread_ts,
                    text=text,
                )
            else:
                resp = self.client.chat_postMessage(
                    channel=session.channel_id, text=text
                )
                session.thread_ts = resp["ts"]
        except Exception as e:
            logger.error("Slack post error: %s", e)

    def _upload_to_session(self, session: Session, content: str,
                           *, filename: str = "response.md"):
        """セッション（スレッド）にファイルをアップロード"""
        try:
            self.client.files_upload_v2(
                channel=session.channel_id,
                thread_ts=session.thread_ts or None,
                content=content,
                filename=filename,
                title=t("task_full_text_title"),
            )
        except Exception as e:
            logger.error("Slack file upload error: %s", e)

    def _post_completion(self, session: Session, task: Task,
                         text: str, inst: dict | None,
                         *, full_result: str | None = None,
                         blocks: list[dict] | None = None):
        """完了メッセージを投稿 + 添付ファイル送信。
        blocks 指定時は chat_postMessage で本文を blocks 形式で投稿し、ファイルは別投稿で添付する
        （files_upload_v2 の initial_comment は blocks 非対応のため2リクエストに分割）。"""
        # 添付ファイル準備
        status_history = inst.get("_status_history", []) if inst else []
        file_uploads: list[dict] = []
        if full_result:
            file_uploads.append({
                "content": full_result,
                "filename": f"result_{task.short_id}.md",
                "title": t("task_full_text_title"),
            })
        if len(status_history) >= 1:
            snippet_content = "\n".join(status_history)
            file_uploads.append({
                "content": snippet_content,
                "filename": f"progress_{task.short_id}.txt",
                "title": t("status_history_title"),
            })
        # Edit差分ファイル
        if task.file_diffs:
            diff_content = "\n\n".join(task.file_diffs)
            file_uploads.append({
                "content": diff_content,
                "filename": f"changes_{task.short_id}.diff",
                "title": "Changes",
            })

        if blocks:
            # blocks付き: 本文を chat_postMessage で投稿、添付ファイルは別投稿
            try:
                self.client.chat_postMessage(
                    channel=session.channel_id,
                    thread_ts=session.thread_ts or None,
                    text=text,
                    blocks=blocks,
                )
            except Exception as e:
                logger.error("Completion blocks post error: %s", e)
                self._post_to_session(session, text)
            if file_uploads:
                try:
                    self.client.files_upload_v2(
                        channel=session.channel_id,
                        thread_ts=session.thread_ts or None,
                        file_uploads=file_uploads,
                    )
                except Exception as e:
                    logger.error("Completion file upload error: %s", e)
            if task.result and task.status == TaskStatus.COMPLETED:
                self._check_tool_request(session, task)
            return

        # blocks 未指定（エラー系等）: 従来通り initial_comment + ファイル添付
        if file_uploads:
            try:
                self.client.files_upload_v2(
                    channel=session.channel_id,
                    thread_ts=session.thread_ts or None,
                    initial_comment=text,
                    file_uploads=file_uploads,
                )
                # ツールリクエストマーカー検出（ファイルアップロード完了後）
                if task.result and task.status == TaskStatus.COMPLETED:
                    self._check_tool_request(session, task)
                return
            except Exception as e:
                logger.error("Completion file upload error: %s", e)
                # フォールバック: テキストのみ新規投稿
        # ファイルアップロードなし or 失敗時: 新規メッセージとして投稿（通知を発生させる）
        self._post_to_session(session, text)
        # ツールリクエストマーカー検出（完了メッセージ投稿後）
        if task.result and task.status == TaskStatus.COMPLETED:
            self._check_tool_request(session, task)
        return

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """ツール名を --allowedTools 互換形式に正規化。
        Bash(cmd pattern) はそのまま保持。それ以外のツール（Grep, Read 等）は
        括弧付き引数（パス等）を除去してベース名のみにする。
        例: Grep(/Users/foo) → Grep, Bash(npm install) → Bash(npm install)
        """
        paren_idx = name.find("(")
        if paren_idx < 0:
            return name
        base = name[:paren_idx]
        if base == "Bash":
            return name  # Bash(pattern) は CLI がサポートする形式
        return base

    def _check_tool_request(self, session: Session, task: Task):
        """タスク結果から [TOOL_REQUEST:...] マーカーを検出し、ボタン付きメッセージを投稿"""
        matches = _TOOL_REQUEST_RE.findall(task.result or "")
        if not matches:
            return
        # Bash 以外のツールは括弧部分を除去して正規化
        normalized = [self._normalize_tool_name(m) for m in matches]
        requested_tools = list(dict.fromkeys(normalized))  # 重複排除、順序保持
        logger.info("_check_tool_request: detected tool requests=%s thread=%s",
                     requested_tools, session.thread_ts)
        session.pending_tool_request = {
            "tools": requested_tools,
            "user_id": task.user_id,
        }
        tools_display = ", ".join(f"`{t_}`" for t_ in requested_tools)
        # コンテキスト情報を構築（作業ディレクトリ）
        dir_name = os.path.basename(session.working_dir) if session.working_dir else ""
        context_line = t("tool_request_context", dir=dir_name) if dir_name else ""
        msg_text = t("tool_request_message", tools=tools_display)
        if context_line:
            msg_text += "\n" + context_line
        msg_text += "\n" + t("tool_request_message_suffix")
        # ボタンのvalueにJSON埋め込み（セッション識別用）
        value_data = json.dumps({
            "thread_ts": session.thread_ts,
            "channel_id": session.channel_id,
            "tools": requested_tools,
        })
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": msg_text,
                },
            },
            {
                "type": "input",
                "block_id": "tool_request_comment",
                "optional": True,
                "label": {"type": "plain_text", "text": t("tool_request_comment_label")},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "comment",
                    "multiline": True,
                },
            },
            {
                "type": "divider",
            },
            {
                "type": "actions",
                "block_id": "tool_request_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": t("tool_request_approve_once")},
                        "style": "primary",
                        "action_id": "tool_request_approve_once",
                        "value": value_data,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": t("tool_request_approve_session")},
                        "action_id": "tool_request_approve_session",
                        "value": value_data,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": t("tool_request_approve_project")},
                        "action_id": "tool_request_approve_project",
                        "value": value_data,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": t("tool_request_reject")},
                        "style": "danger",
                        "action_id": "tool_request_reject",
                        "value": value_data,
                    },
                ],
            },
        ]
        try:
            self.client.chat_postMessage(
                channel=session.channel_id,
                thread_ts=session.thread_ts,
                text=msg_text,
                blocks=blocks,
            )
        except Exception as e:
            logger.error("Tool request button post error: %s", e)

        # 進捗リアクションを「ツール許可待ち」に上書き
        # （直前で _swap_task_reaction(REACTION_COMPLETED) されているので、bell に差し替え）
        self._swap_task_reaction(task, REACTION_AWAITING_TOOL)

    def _cleanup_status_message(self, inst: dict | None, session: Session, task: Task):
        """タスク完了時: 進捗メッセージを削除"""
        if not inst:
            logger.debug("_cleanup_status_message: inst is None, skipping thread=%s", session.thread_ts)
            return
        status_msg_ts = inst.get("_status_msg_ts")
        # 進捗メッセージを削除（ベストエフォート）
        if status_msg_ts:
            try:
                self.client.chat_delete(
                    channel=session.channel_id,
                    ts=status_msg_ts,
                )
            except Exception as e:
                logger.debug("Status message delete failed (best-effort): %s", e)

    # ── キャンセル ──
    def cancel_task(self, task_id: int) -> bool:
        """グローバル検索でタスクをキャンセル"""
        result = self.find_task_globally(task_id)
        if not result:
            return False
        _, _, task = result
        if task.status != TaskStatus.RUNNING:
            return False
        task.status = TaskStatus.CANCELLED
        if task.process:
            try:
                task.process.terminate()
                task.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                task.process.kill()
        return True

    def cancel_all_in_project(self, channel_id: str) -> int:
        """プロジェクト内の全アクティブタスクをキャンセル"""
        project = self.projects.get(channel_id)
        if not project:
            return 0
        cancelled = 0
        for session in project.sessions.values():
            active = session.active_task
            if active and active.status == TaskStatus.RUNNING:
                active.status = TaskStatus.CANCELLED
                if active.process:
                    try:
                        active.process.terminate()
                        active.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        active.process.kill()
                cancelled += 1
        return cancelled

    def cancel_all(self) -> int:
        """全プロジェクトの全タスクをキャンセル（シャットダウン用）"""
        cancelled = 0
        for channel_id in list(self.projects.keys()):
            cancelled += self.cancel_all_in_project(channel_id)
        return cancelled

    def has_running_tasks(self) -> bool:
        """実行中（RUNNING かつプロセス生存）のタスクが1つでもあるか。
        Socket Mode 連続エラー時に「再起動すると走行中の作業を巻き添えに殺す」かどうかの
        判断に使う。タスクはサブプロセス+JSONL監視で動作し Slack socket には依存しないため、
        接続不良中でも走り続けられる。"""
        for project in self.projects.values():
            for session in project.sessions.values():
                active = session.active_task
                if (active and active.status == TaskStatus.RUNNING
                        and active.process and active.process.poll() is None):
                    return True
        return False

    def all_running_tasks_stale(self, threshold: float) -> bool:
        """走行中タスクが1つ以上あり、その全てが threshold 秒以上 JSONL 無活動か。
        プロセスは生存していても JSONL が長時間無更新なら「ハング」とみなす。
        socket wedge 時の再起動判断で使い、ハングタスクが has_running_tasks() の
        再起動ガードを人質に取って死んだ socket を永久に復旧できなくする事故を防ぐ。
        1つでも活動中のタスクがあれば False（巻き添えにしない）。"""
        now = time.time()
        found = False
        for project in self.projects.values():
            for session in project.sessions.values():
                active = session.active_task
                if (active and active.status == TaskStatus.RUNNING
                        and active.process and active.process.poll() is None):
                    found = True
                    if (now - active.last_activity) < threshold:
                        return False
        return found


def _capture_edit_diff(task: Task, tool_name: str, tool_input: dict):
    """Edit/MultiEdit ツール入力からunified diff形式の差分を生成しタスクに蓄積"""
    edits = []
    if tool_name == "Edit":
        fp = tool_input.get("file_path", "?")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if old or new:
            edits.append((fp, old, new))
    elif tool_name == "MultiEdit":
        fp = tool_input.get("file_path", "?")
        for edit in tool_input.get("edits", []):
            if isinstance(edit, dict):
                old = edit.get("old_string", "")
                new = edit.get("new_string", "")
                if old or new:
                    edits.append((fp, old, new))
    for fp, old, new in edits:
        diff_lines = [f"--- a/{fp}", f"+++ b/{fp}", "@@ edit @@"]
        for line in old.splitlines():
            diff_lines.append(f"-{line}")
        for line in new.splitlines():
            diff_lines.append(f"+{line}")
        task.file_diffs.append("\n".join(diff_lines))


def _summarize_input(input_data: dict) -> str:
    if not input_data:
        return ""
    if "file_path" in input_data:
        return input_data["file_path"]
    if "command" in input_data:
        cmd = input_data["command"]
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if "pattern" in input_data:
        return input_data["pattern"]
    if "query" in input_data:
        q = input_data["query"]
        return q[:80] + ("..." if len(q) > 80 else "")
    if "url" in input_data:
        return input_data["url"][:80]
    if "notebook_path" in input_data:
        return input_data["notebook_path"]
    return str(input_data)[:80]


# ── Slack App ─────────────────────────────────────────────
app = App(token=SLACK_BOT_TOKEN)
slack_client = WebClient(token=SLACK_BOT_TOKEN)
runner = ClaudeCodeRunner(slack_client)

# 必要な Bot Token スコープ（起動時に付与状況を検証する）。
# setup-guide.md の Bot Token Scopes と一致させること。
REQUIRED_BOT_SCOPES = [
    "chat:write",
    "channels:history",
    "groups:history",
    "files:read",
    "files:write",
    "reactions:write",
]


def _check_bot_scopes():
    """起動時に Bot Token の付与済みスコープを検証する。
    必要なスコープが欠けていれば warning ログ + Slack 通知
    （NOTIFICATION_CHANNEL、なければ管理者へDM）で再認可を促す。
    検証不能なとき（auth_test 失敗・ヘッダ欠落）は何もしないベストエフォート。"""
    try:
        resp = slack_client.auth_test()
    except Exception as e:
        logger.warning("スコープ検証をスキップ（auth_test 失敗）: %s", e)
        return
    # 付与済みスコープは x-oauth-scopes レスポンスヘッダにカンマ区切りで入る
    headers = getattr(resp, "headers", None) or {}
    granted_raw = next(
        (v for k, v in headers.items() if k.lower() == "x-oauth-scopes"), None
    )
    if not granted_raw:
        logger.debug("スコープ検証をスキップ: x-oauth-scopes ヘッダなし")
        return
    granted = {s.strip() for s in granted_raw.split(",") if s.strip()}
    missing = [s for s in REQUIRED_BOT_SCOPES if s not in granted]
    if not missing:
        logger.info("Bot スコープ OK（%d 件付与）", len(granted))
        return
    missing_str = ", ".join(missing)
    logger.warning("Bot Token のスコープ不足: %s — 再認可が必要です", missing_str)
    target = NOTIFICATION_CHANNEL or ADMIN_SLACK_USER_ID
    try:
        slack_client.chat_postMessage(
            channel=target, text=t("notify_missing_scopes", scopes=missing_str)
        )
    except Exception as e:
        logger.warning("スコープ不足通知の送信に失敗: %s", e)

# スレッドts → インスタンス情報のマッピング（起動時に検出したclaude CLIプロセス用）
instance_threads: dict[str, dict] = {}

# 現在監視中のJSONLファイルパス（重複監視防止）
_monitored_jsonl_paths: set[str] = set()

# fork の複数候補選択状態（channel_id → 選択情報）
pending_fork_selections: dict[str, dict] = {}

# bind の複数候補選択状態（channel_id → 選択情報）
pending_bind_selections: dict[str, dict] = {}

# ベアタスクのディレクトリ選択待ち状態（thread_ts → 選択情報）
pending_directory_requests: dict[str, dict] = {}

# シャットダウンイベント（バックグラウンドスレッド停止用）
_shutdown_event = threading.Event()

# メッセージ重複排除キャッシュ（Socket Mode再接続時の再送対策）
# イベントの client_msg_id または ts をキーとして保持
_DEDUP_CACHE_MAX = 500
_processed_events: collections.OrderedDict[str, None] = collections.OrderedDict()
_processed_events_lock = threading.Lock()


BOT_USER_ID = None


def get_bot_user_id():
    global BOT_USER_ID
    if BOT_USER_ID is None:
        resp = slack_client.auth_test()
        BOT_USER_ID = resp["user_id"]
    return BOT_USER_ID


def parse_task_id(s: str) -> Optional[int]:
    m = re.match(r"#?(\d+)", s.strip())
    return int(m.group(1)) if m else None


def _strip_bot_mention(text: str) -> str:
    """テキストから <@BOT_ID> を除去。除去しなかった場合は元のテキストをそのまま返す"""
    bot_id = get_bot_user_id()
    return re.sub(rf'<@{bot_id}>\s*', '', text).strip()


@app.event("message")
def handle_message(event, say):
    # ── メッセージ重複排除（Socket Mode再接続時の再送対策）──
    dedup_key = event.get("client_msg_id") or event.get("ts", "")
    if dedup_key:
        with _processed_events_lock:
            if dedup_key in _processed_events:
                logger.info("Duplicate message ignored: key=%s", dedup_key)
                return
            _processed_events[dedup_key] = None
            # キャッシュサイズ制限
            while len(_processed_events) > _DEDUP_CACHE_MAX:
                _processed_events.popitem(last=False)

    channel_type = event.get("channel_type", "")
    user_id = event.get("user", "")

    # botメッセージは常に無視
    if event.get("bot_id") or user_id == get_bot_user_id():
        return

    if channel_type == "im":
        # DMモードは廃止（チャンネルモードのみ対応）
        return

    elif channel_type in ("channel", "group"):
        # ── チャンネルモード ──
        channel_id = event.get("channel", "")

        # チャンネル許可チェック
        if not _is_channel_allowed(channel_id):
            return

        # ユーザー許可チェック
        if not _is_user_allowed(user_id):
            return

        text = event.get("text", "").strip()
        files = event.get("files", [])
        if not text and not files:
            return

        parent_ts = event.get("thread_ts")

        # スレッド返信の処理
        if parent_ts:
            logger.info("Thread reply received: thread=%s user=%s text_len=%d channel=%s",
                        parent_ts, user_id, len(text), channel_id)
            # 1. instance_threads に登録あり & アクティブタスク実行中 → PTY に入力転送
            if parent_ts in instance_threads:
                logger.info("Thread reply: routing to instance_threads (PTY) thread=%s", parent_ts)
                # メンション付きならコマンド判定を優先
                stripped = _strip_bot_mention(text)
                if stripped != text:
                    cmd_lower = stripped.lower()
                    if cmd_lower == "cancel":
                        _handle_cancel_in_thread(say, parent_ts, channel_id)
                        return
                    if cmd_lower.startswith("cancel "):
                        # cancel <指示>: 現タスクを中断し補足付きで追加指示を即発火
                        instr = stripped[7:].strip()
                        session = runner.get_or_create_project(channel_id).sessions.get(parent_ts)
                        if instr and session:
                            _enqueue_followup(session, instr, user_id,
                                              _resolve_event_files(event, channel_id),
                                              event.get("ts"), "cancel", say, parent_ts)
                            return
                    if cmd_lower == "status":
                        _handle_status(say, parent_ts, channel_id)
                        return
                    if cmd_lower.startswith("tools "):
                        session = runner.get_or_create_project(channel_id).sessions.get(parent_ts)
                        if session:
                            tools = stripped[6:].strip()
                            session.next_tools = tools
                            say(text=t("tools_set", tools=tools), thread_ts=parent_ts)
                            return
                input_text = stripped if stripped != text else text
                input_text = "/" + input_text[1:] if input_text.startswith("!") else input_text
                handled = _handle_instance_input(input_text, say, parent_ts, channel_id,
                                                _resolve_event_files(event, channel_id),
                                                user_id, event.get("ts"))
                if handled:
                    return
                logger.info("Thread reply: instance_input returned False (EIO), falling through thread=%s", parent_ts)
                # EIOでFalse返却 → instance_threads除去済み、セッション --resume パスへフォールスルー

            # 1.5. ディレクトリ選択待ち → 選択処理
            if parent_ts in pending_directory_requests:
                input_text = _strip_bot_mention(text)
                if not input_text:
                    input_text = text
                if _handle_directory_selection(input_text, say, parent_ts):
                    return

            # 2. セッション存在 → --resume で新タスク自動作成（メンション不要）
            #    get_or_create_project を使い、再起動後も sessions.json から復元する
            project = runner.get_or_create_project(channel_id)
            session = project.sessions.get(parent_ts)
            logger.info("Thread reply: session lookup thread=%s found=%s project_sessions=%d",
                        parent_ts, session is not None, len(project.sessions))
            if session:
                logger.info("Thread reply: session found thread=%s claude_session_id=%s tasks=%d active=%s",
                            parent_ts, session.claude_session_id[:16] if session.claude_session_id else "None",
                            len(session.tasks), session.active_task is not None)
                stripped = _strip_bot_mention(text)
                is_mentioned = (stripped != text)
                input_text = stripped if is_mentioned else text
                if not input_text:
                    input_text = text  # メンションのみの場合は元テキストを使用
                # コマンド判定（メンション付きの場合のみ）
                if is_mentioned:
                    cmd_lower = input_text.lower()
                    if cmd_lower.startswith("tools "):
                        tools = input_text[6:].strip()
                        session.next_tools = tools
                        say(
                            text=t("tools_set", tools=tools),
                            thread_ts=parent_ts,
                        )
                        return
                    if cmd_lower == "cancel":
                        _handle_cancel_in_thread(say, parent_ts, channel_id)
                        return
                    if cmd_lower.startswith("cancel "):
                        # cancel <指示>: 実行中なら中断して即発火、idleならそのまま新タスク
                        instr = input_text[7:].strip()
                        if instr and session.active_task:
                            _enqueue_followup(session, instr, user_id,
                                              _resolve_event_files(event, channel_id),
                                              event.get("ts"), "cancel", say, parent_ts)
                            return
                        input_text = instr or input_text
                    if cmd_lower == "status":
                        _handle_status(say, parent_ts, channel_id)
                        return
                # 実行中タスクがあれば追加指示としてキュー（完走後に --resume 発火）
                if session.active_task and session.active_task.status == TaskStatus.RUNNING:
                    _enqueue_followup(session, input_text, user_id,
                                      _resolve_event_files(event, channel_id),
                                      event.get("ts"), "queue", say, parent_ts)
                    return
                # 新タスクとして --resume 続行（メンション有無問わず）
                resolved_files = _resolve_event_files(event, channel_id)
                _handle_thread_reply_task(
                    input_text, project, session, say, parent_ts, user_id, resolved_files,
                    request_msg_ts=event.get("ts"),
                )
                return

            # フォールバック: メンション付きならコマンドとして処理
            stripped = _strip_bot_mention(text)
            if stripped != text:
                # メンション付きスレッド返信 → コマンドとして処理
                logger.info("Thread reply: session NOT found, processing as mentioned command thread=%s channel=%s",
                            parent_ts, channel_id)
                _dispatch_command(stripped, event, say)
            else:
                logger.info("Thread reply: session NOT found and no mention, IGNORING thread=%s channel=%s",
                            parent_ts, channel_id)
            return

        # フォーク選択割り込み（メンション有無問わず、番号 or cancel のみ処理）
        if channel_id in pending_fork_selections:
            fork_input = _strip_bot_mention(text)
            if fork_input == text:
                fork_input = text  # メンションなしでもOK
            if _handle_fork_selection(fork_input, say, channel_id):
                return

        # バインド選択割り込み
        if channel_id in pending_bind_selections:
            bind_input = _strip_bot_mention(text)
            if bind_input == text:
                bind_input = text
            if _handle_bind_selection(bind_input, say, channel_id):
                return

        # トップレベルメッセージ: botメンション必須
        stripped = _strip_bot_mention(text)
        if stripped == text:
            return  # メンションなし → 無視
        text = stripped
        if not text and not files:
            return

        _dispatch_command(text, event, say)


def _build_question_answer_prompt(question_text: str, options: list[dict],
                                  selected_num: int | None, answer_label: str) -> str:
    """質問への回答プロンプトを構築。--resume なしでもClaude が文脈を理解できるよう質問全体を含める。"""
    parts = []
    if question_text:
        parts.append(t("prompt_answer_to_question", question=question_text))
    else:
        parts.append(t("prompt_answer_to_prev"))
    if options:
        opts = "\n".join(
            f"  {i+1}. {o.get('label', '')}" + (f" — {o['description']}" if o.get('description') else "")
            for i, o in enumerate(options)
        )
        parts.append(f"{t('prompt_options_label')}\n{opts}")
    if selected_num is not None:
        parts.append(t("prompt_answer_numbered", num=selected_num, label=answer_label))
    else:
        parts.append(t("prompt_answer_text", label=answer_label))
    return "\n".join(parts)


def _handle_thread_reply_task(prompt: str, project: Project, session: Session,
                              say, thread_ts: str, user_id: str,
                              files: list[dict] | None = None,
                              request_msg_ts: str | None = None):
    """スレッド返信を --resume で新タスクとして実行"""
    # pending_question がある場合、回答を質問コンテキスト付きプロンプトに変換
    pq = session.pending_question
    is_plan_approval = pq.get("is_plan_approval", False) if pq else False
    plan_approved = False

    if pq and not pq.get("multi_select", False):
        options = pq.get("options", [])
        question_text = pq.get("question", "")
        num_match = re.match(r"^\s*(\d+)\s*$", prompt)
        if num_match:
            num = int(num_match.group(1))
            if 1 <= num <= len(options):
                label = options[num - 1].get("label", prompt)
                prompt = _build_question_answer_prompt(question_text, options, num, label)
                # プラン承認: 選択肢1 = 承認
                if is_plan_approval and num == 1:
                    plan_approved = True
        else:
            # テキスト回答の場合もコンテキスト付与
            prompt = _build_question_answer_prompt(question_text, options, None, prompt)
            # テキスト回答 = フィードバック（却下扱い）
        session.pending_question = None

    # 添付ファイルのダウンロードとプロンプト拡張
    if files and session.working_dir:
        file_paths = _download_slack_files(files, session.working_dir)
        if file_paths:
            prompt = _augment_prompt_with_files(prompt, file_paths)

    # セッションにclaude_session_idがまだ設定されていない場合、短時間待機
    # （前タスクの_executeがJSONL処理中の可能性があるため）
    if not session.claude_session_id and session.tasks:
        logger.debug("_handle_thread_reply_task: session_id not set, starting wait thread=%s tasks=%d", thread_ts, len(session.tasks))
        for wait_i in range(10):
            time.sleep(0.3)
            if session.claude_session_id:
                logger.debug("_handle_thread_reply_task: session_id acquired during wait sid=%s wait=%.1fs", session.claude_session_id[:16], (wait_i+1)*0.3)
                break
        else:
            logger.warning("_handle_thread_reply_task: session_id not acquired after 3s wait thread=%s", thread_ts)

    # disallowed_tools の決定: プラン承認時はExitPlanModeを許可
    disallowed = None  # デフォルト: AskUserQuestion,ExitPlanMode 両方無効
    if plan_approved:
        disallowed = "AskUserQuestion"  # ExitPlanModeを許可
        logger.info("_handle_thread_reply_task: plan approved, allowing ExitPlanMode thread=%s", thread_ts)

    task = Task(
        id=0,
        prompt=prompt,
        allowed_tools=session.consume_tools(),
        disallowed_tools=disallowed,
        user_id=user_id,
    )
    # セッションにclaude_session_idがあれば自動で --resume
    if session.claude_session_id:
        task.resume_session = session.claude_session_id
        # pending_fork: fork登録後の最初のタスクに --fork-session を付与
        if session.consume_fork():
            task.fork_session = True
            logger.info("_handle_thread_reply_task: fork-session for sid=%s thread=%s", session.claude_session_id[:16], thread_ts)
        logger.info("_handle_thread_reply_task: creating task with --resume sid=%s thread=%s", session.claude_session_id[:16], thread_ts)
    else:
        logger.warning("_handle_thread_reply_task: no session_id, running new task without --resume thread=%s", thread_ts)

    # 受付リアクション
    runner.assign_task_id(task)
    task.reaction_targets = _build_reaction_targets(
        session.channel_id, request_msg_ts, thread_ts,
    )
    runner._swap_task_reaction(task, REACTION_RECEIVED)

    logger.info("_handle_thread_reply_task: calling run_task thread=%s resume=%s disallowed=%s",
                thread_ts, task.resume_session[:16] if task.resume_session else "None", task.disallowed_tools)
    err = runner.run_task(project, session, task)
    if err:
        logger.warning("_handle_thread_reply_task: run_task returned error=%s thread=%s", err, thread_ts)
        say(text=err, thread_ts=thread_ts)


def _dispatch_command(text: str, event: dict, say):
    """コマンド解析・実行"""
    channel_id = event.get("channel", "")
    thread_ts = event.get("ts")
    user_id = event.get("user", "")
    request_msg_ts = event.get("ts")

    cmd_lower = text.lower()

    # ── help ──
    if cmd_lower in ("help", "?"):
        say(text=_help_text(), thread_ts=thread_ts)
        return

    # ── status ──
    if cmd_lower == "status":
        _handle_status(say, thread_ts, channel_id)
        return

    # ── cancel ──
    if cmd_lower == "cancel":
        _handle_cancel(text, say, thread_ts, channel_id)
        return

    # ── sessions ──
    if cmd_lower == "sessions":
        _handle_sessions(say, thread_ts, channel_id)
        return

    # ── fork [<PID>] [<task>] ──
    if cmd_lower == "fork" or cmd_lower.startswith("fork "):
        rest = text[4:].strip()
        if not rest:
            _handle_fork_list(say, thread_ts, channel_id, user_id)
        else:
            _handle_fork(rest, say, thread_ts, channel_id, user_id,
                         _resolve_event_files(event, channel_id),
                         request_msg_ts=request_msg_ts)
        return

    # ── bind [<PID>] ──
    if cmd_lower == "bind" or cmd_lower.startswith("bind "):
        rest = text[4:].strip()
        if not rest:
            _handle_bind_list(say, thread_ts, channel_id, user_id)
        else:
            _handle_bind(rest, say, thread_ts, channel_id, user_id)
        return

    # ── tools <list> （トップレベルではエラー） ──
    if cmd_lower.startswith("tools "):
        say(
            text=t("error_tools_thread_only"),
            thread_ts=thread_ts,
        )
        return

    # ── continue / resume （廃止） ──
    if cmd_lower.startswith("continue") or cmd_lower.startswith("resume "):
        say(
            text=t("error_continue_deprecated"),
            thread_ts=thread_ts,
        )
        return

    # ── team [in <path>] <タスク> ──
    if cmd_lower == "team" or cmd_lower.startswith("team "):
        rest = text[4:].strip()
        _handle_team(rest, say, thread_ts, channel_id, user_id,
                     _resolve_event_files(event, channel_id),
                     request_msg_ts=request_msg_ts)
        return

    # ── root [<path>|clear] ──
    if cmd_lower == "root" or cmd_lower.startswith("root "):
        _handle_root(text, say, thread_ts, channel_id)
        return

    # ── in <path> <タスク> ──
    if cmd_lower.startswith("in "):
        _handle_in_dir(text, say, thread_ts, channel_id, user_id,
                       _resolve_event_files(event, channel_id),
                       request_msg_ts=request_msg_ts)
        return

    # ── ベアタスク → ディレクトリ選択 or 履歴から自動 ──
    _handle_bare_task(text, event, say, channel_id, user_id)


def _build_terminal_focus_script(tty_device: str) -> str:
    """ターミナルタブにフォーカスするAppleScript部分。
    変数 `found` をセットし、後続スクリプトで参照可能。"""
    return f'''
tell application "Terminal"
    activate
    delay 0.3
    set found to false
    repeat with w in windows
        repeat with t in tabs of w
            if tty of t is "{tty_device}" then
                set frontmost of w to true
                delay 0.1
                set selected of t to true
                set found to true
                exit repeat
            end if
        end repeat
        if found then exit repeat
    end repeat
end tell
'''


def _build_paste_script(tty_device: str) -> str:
    """既存のペースト→Esc→Enter操作のAppleScript"""
    focus = _build_terminal_focus_script(tty_device)
    return focus + '''
if found then
    delay 0.3
    tell application "System Events"
        tell process "Terminal"
            keystroke "v" using command down
            delay 0.3
            key code 53
            delay 0.1
            keystroke return
        end tell
    end tell
end if
'''


def _build_select_option_script(tty_device: str, option_index: int) -> str:
    """選択肢を矢印キーで選択するAppleScript。
    option_index: 0始まりのインデックス（0=1番目=初期選択なのでそのままEnter）"""
    focus = _build_terminal_focus_script(tty_device)
    if option_index == 0:
        # 1番目が初期選択なのでEnterのみ
        return focus + '''
if found then
    delay 0.3
    tell application "System Events"
        tell process "Terminal"
            keystroke return
        end tell
    end tell
end if
'''
    else:
        return focus + f'''
if found then
    delay 0.3
    tell application "System Events"
        tell process "Terminal"
            repeat {option_index} times
                key code 125
                delay 0.05
            end repeat
            delay 0.1
            keystroke return
        end tell
    end tell
end if
'''


def _build_select_other_script(tty_device: str, option_count: int) -> str:
    """Other選択→テキストペースト→EnterのAppleScript。
    option_count回の下矢印でOtherを選択し、Enter→テキストペースト→Enter。"""
    focus = _build_terminal_focus_script(tty_device)
    return focus + f'''
if found then
    delay 0.3
    tell application "System Events"
        tell process "Terminal"
            repeat {option_count} times
                key code 125
                delay 0.05
            end repeat
            delay 0.1
            keystroke return
            delay 0.5
            keystroke "v" using command down
            delay 0.3
            keystroke return
        end tell
    end tell
end if
'''


def _enqueue_followup(session: "Session", text: str, user_id: str,
                      files: list[dict] | None, request_msg_ts: str | None,
                      mode: str, say, thread_ts: str):
    """実行中タスクへの追加指示を pending_followup に積む。
    mode='queue': 現タスクを完走させ、完了時(_executeのfinally)に --resume 発火。
    mode='cancel': 既存キューを破棄し、現タスクを中断して、この指示だけを補足付きで即発火。
    発火自体は _execute の finally → _fire_one_followup が行う（競合回避）。"""
    if not text or not text.strip():
        return
    entry = {
        "text": text,
        "user_id": user_id or "",
        "files": files or [],
        "request_msg_ts": request_msg_ts,
        "mode": mode,
    }
    if mode == "cancel":
        # cancel <指示>: 「中断して今すぐこれを」の意。引数なし cancel と同様に
        # 既存キューも破棄し、この指示のみを中断後に即発火する。
        with runner.lock:
            dropped = len(session.pending_followup)
            session.pending_followup.clear()
            session.pending_followup.append(entry)
        msg = t("followup_queued_cancel")
        if dropped:
            msg += " " + t("followup_dropped", count=dropped)
        say(text=msg, thread_ts=thread_ts)
        active = session.active_task
        if active and active.status == TaskStatus.RUNNING:
            runner.cancel_task(active.id)
    else:
        with runner.lock:
            session.pending_followup.append(entry)
            qlen = len(session.pending_followup)
        say(text=t("followup_queued", count=qlen), thread_ts=thread_ts)


def _handle_instance_input(text: str, say, parent_ts: str, channel_id: str,
                           files: list[dict] | None = None,
                           user_id: str = "", request_msg_ts: str | None = None) -> bool:
    """インスタンススレッドへの返信をPTY書き込みまたはクリップボード+ペースト経由でターミナルに転送。
    pending_questionがある場合は選択肢回答として処理する。
    質問待ちでない実行中タスクへのフリーテキストは pending_followup に積む（完了後に --resume 発火）。
    戻り値: True=処理済み, False=EIOでinstance_threadsから除去済み（呼び出し元でフォールスルーすべき）"""
    inst = instance_threads[parent_ts]

    # 添付ファイルがある場合、ダウンロードしてテキストにパスを追記
    if files:
        cwd = inst.get("cwd", os.getcwd())
        file_paths = _download_slack_files(files, cwd)
        if file_paths:
            text = _augment_prompt_with_files(text, file_paths)
    tty = inst.get("tty", "")
    pid = inst["pid"]
    master_fd = inst.get("master_fd")
    pending_q = inst.get("pending_question")

    try:
        # ── PTYモード: master_fdに直接書き込み ──
        if master_fd is not None:
            action_label = None

            if pending_q and not pending_q.get("multi_select", False):
                options = pending_q.get("options", [])
                option_count = len(options)

                num_match = re.match(r"^\s*(\d+)\s*$", text)
                if num_match:
                    num = int(num_match.group(1))
                    if 1 <= num <= option_count:
                        # Down矢印で選択肢にナビゲート + Enter
                        pty_input = b"\x1b[B" * (num - 1) + b"\r"
                        os.write(master_fd, pty_input)
                        action_label = t("input_selected_option", num=num, label=options[num - 1].get('label', ''))
                    else:
                        say(text=t("error_enter_number_range", max=option_count), thread_ts=parent_ts)
                        return True
                else:
                    # テキスト入力 → "Other" を選択してテキスト入力
                    pty_input = b"\x1b[B" * option_count + b"\r"
                    os.write(master_fd, pty_input)
                    time.sleep(0.5)
                    os.write(master_fd, text.encode("utf-8") + b"\r")
                    action_label = f"Other: {text[:50]}"

                inst.pop("pending_question", None)
                session_ref = inst.get("session")
                if session_ref:
                    session_ref.pending_question = None
            elif pending_q and pending_q.get("multi_select", False):
                # multi_select質問へのテキスト回答
                inst.pop("pending_question", None)
                session_ref = inst.get("session")
                if session_ref:
                    session_ref.pending_question = None
                os.write(master_fd, text.encode("utf-8") + b"\r")
            elif inst.get("bind_mode") == "live":
                # バインドモード: フリーテキスト入力を許可
                os.write(master_fd, text.encode("utf-8") + b"\r")
                action_label = None
            else:
                # 質問待ちでない → 実行中タスクへの追加指示。キューに積み完了後に --resume 発火
                session_ref = inst.get("session")
                if session_ref is not None:
                    _enqueue_followup(session_ref, text, user_id, None,
                                      request_msg_ts, "queue", say, parent_ts)
                else:
                    say(text=t("input_blocked_task_running"), thread_ts=parent_ts)
                return True

            # Slack通知
            if action_label:
                msg_text = t("input_answer_sent", pid=pid, label=action_label)
            else:
                msg_text = t("input_sent", pid=pid)
            resp = slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=parent_ts,
                text=msg_text,
            )
            inst["input_msg_ts"] = resp["ts"]
            inst["input_msg_text"] = msg_text
            w = inst.get("_watcher")
            if w:
                w.notify()
            return True

        # ── TTYがない場合のエラー ──
        if not tty:
            say(text=t("error_pid_no_tty", pid=pid), thread_ts=parent_ts)
            return True

        # ── 既存のAppleScript処理（外部検出インスタンス用） ──
        tty_device = f"/dev/{tty}"

        if pending_q and not pending_q.get("multi_select", False):
            # 選択肢回答モード
            options = pending_q.get("options", [])
            option_count = len(options)

            # 番号入力チェック
            num_match = re.match(r"^\s*(\d+)\s*$", text)
            if num_match:
                num = int(num_match.group(1))
                if 1 <= num <= option_count:
                    # 番号で選択肢を選ぶ（0始まりインデックス）
                    script = _build_select_option_script(tty_device, num - 1)
                    action_label = t("input_selected_option", num=num, label=options[num - 1].get('label', ''))
                else:
                    say(text=t("error_enter_number_range", max=option_count), thread_ts=parent_ts)
                    return True
            else:
                # テキスト入力 → "Other" を選択してペースト
                subprocess.run(
                    ["pbcopy"],
                    input=text.encode("utf-8"),
                    timeout=5,
                )
                script = _build_select_other_script(tty_device, option_count)
                action_label = f"Other: {text[:50]}"

            # pending_question をクリア
            inst.pop("pending_question", None)
            session_ref = inst.get("session")
            if session_ref:
                session_ref.pending_question = None
        elif pending_q and pending_q.get("multi_select", False):
            # multi_select質問へのテキスト回答
            inst.pop("pending_question", None)
            session_ref = inst.get("session")
            if session_ref:
                session_ref.pending_question = None

            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                timeout=5,
            )
            script = _build_paste_script(tty_device)
            action_label = None
        elif inst.get("bind_mode") == "live":
            # バインドモード: フリーテキスト入力を許可（クリップボード+ペースト）
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                timeout=5,
            )
            script = _build_paste_script(tty_device)
            action_label = None
        else:
            # 質問待ちでない → タスク実行中なので入力を受け付けない
            say(text=t("input_blocked_task_running"), thread_ts=parent_ts)
            return True

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            if action_label:
                msg_text = t("input_answer_sent", pid=pid, label=action_label)
            else:
                msg_text = t("input_sent", pid=pid)

            resp = slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=parent_ts,
                text=msg_text,
            )
            inst["input_msg_ts"] = resp["ts"]
            inst["input_msg_text"] = msg_text
            w = inst.get("_watcher")
            if w:
                w.notify()
        else:
            say(text=t("error_input_send_failed", error=result.stderr.strip()), thread_ts=parent_ts)
    except OSError as e:
        if e.errno == 5:  # EIO — プロセス終了済み → instance_threadsから除去してフォールスルー
            instance_threads.pop(parent_ts, None)
            return False
        else:
            say(text=t("error_input_send_failed", error=e), thread_ts=parent_ts)
    except Exception as e:
        say(text=t("error_input_send_failed", error=e), thread_ts=parent_ts)
    return True


# Mentionイベントは無視（message.channels で処理済み）
@app.event("app_mention")
def handle_mention(event, say):
    pass


# ── ツール許可ボタンハンドラ ──
def _handle_tool_request_action(ack, body, scope: str):
    """ツール許可リクエストの承認/拒否を処理。
    scope: "once" | "session" | "project" | "reject"
    """
    ack()
    action = body["actions"][0]
    try:
        value = json.loads(action["value"])
    except (json.JSONDecodeError, KeyError):
        logger.error("Tool request action: invalid value")
        return
    thread_ts = value["thread_ts"]
    channel_id = value["channel_id"]
    requested_tools = value["tools"]
    user_id = body.get("user", {}).get("id", "")

    # 同一メッセージ内 input ブロックの現在値（コメント、任意）
    comment = (
        body.get("state", {}).get("values", {})
            .get("tool_request_comment", {}).get("comment", {})
            .get("value") or ""
    ).strip()

    project = runner.get_project(channel_id)
    session = project.sessions.get(thread_ts) if project else None
    if not session:
        logger.warning("Tool request action: session not found thread=%s", thread_ts)
        return

    # 重複クリック防止: pending_tool_request が既にクリアされていたらスキップ
    if session.pending_tool_request is None:
        logger.info("Tool request action: already handled (duplicate click), thread=%s", thread_ts)
        return

    # ボタンメッセージを更新（ボタン除去）
    tools_display = ", ".join(f"`{t_}`" for t_ in requested_tools)
    if scope == "reject":
        update_text = t("tool_request_rejected_with_comment") if comment else t("tool_request_rejected")
    else:
        update_text = t(f"tool_request_approved_{scope}", tools=tools_display)
    if comment:
        update_text += "\n> " + comment.replace("\n", "\n> ")
    try:
        slack_client.chat_update(
            channel=channel_id,
            ts=body["message"]["ts"],
            text=update_text,
            blocks=[],  # ボタンを除去
        )
    except Exception as e:
        logger.error("Tool request button update error: %s", e)

    session.pending_tool_request = None

    # ツール許可待ち中の前タスク（bell リアクション中）。承認/拒否でリアクションを差し替える。
    prev_task = session.tasks[-1] if session.tasks else None

    if scope == "reject":
        if not comment:
            # 却下＋コメント無し: 前タスク自体は正常完了していたので white_check_mark に戻す
            if prev_task:
                runner._swap_task_reaction(prev_task, REACTION_COMPLETED)
            # タスクはここで完全に終了。_execute の finally では pending_tool_request
            # が立っていたため保留されていた追加指示キューを、ここで発火する。
            # （pending_tool_request は上で None 済みなのでガードは通過する）
            try:
                runner._fire_one_followup(project, session)
            except Exception as e:
                logger.warning("_fire_one_followup after reject failed: %s", e)
            return
        # 却下＋コメントあり: コメントをClaudeに伝えるため --resume で再開
        if not session.claude_session_id:
            logger.warning("Tool request reject-with-comment: no session_id for resume thread=%s", thread_ts)
            if prev_task:
                runner._swap_task_reaction(prev_task, REACTION_COMPLETED)
            return
        prompt = t("tool_request_rejected") + "\n\n" + t("tool_request_user_comment", comment=comment)
        task = Task(
            id=0,
            prompt=prompt,
            allowed_tools=DEFAULT_ALLOWED_TOOLS,
            user_id=user_id,
        )
        task.resume_session = session.claude_session_id
        if prev_task:
            inherited_targets = list(prev_task.reaction_targets)
            runner._swap_task_reaction(prev_task, None)
        else:
            inherited_targets = []
        runner.assign_task_id(task)
        task.reaction_targets = inherited_targets
        runner._swap_task_reaction(task, REACTION_RECEIVED)
        err = runner.run_task(project, session, task)
        if err:
            logger.warning("Tool request reject-with-comment: run_task error=%s thread=%s", err, thread_ts)
            try:
                slack_client.chat_postMessage(
                    channel=channel_id, thread_ts=thread_ts, text=err,
                )
            except Exception as e:
                logger.error("Tool request error post: %s", e)
        return

    # スコープに応じてSession/Projectにツールを記憶
    if scope == "session":
        session.approved_tools.update(requested_tools)
        logger.info("Tool request: added to session approved_tools=%s thread=%s",
                     requested_tools, thread_ts)
    elif scope == "project":
        project.approved_tools.update(requested_tools)
        runner.save_project_tools()
        logger.info("Tool request: added to project approved_tools=%s channel=%s",
                     requested_tools, channel_id)

    # 承認: 要求ツールを追加して --resume で再実行
    if not session.claude_session_id:
        logger.warning("Tool request approve: no session_id for resume thread=%s", thread_ts)
        if prev_task:
            runner._swap_task_reaction(prev_task, REACTION_COMPLETED)
        return
    base_tools = DEFAULT_ALLOWED_TOOLS
    added = ",".join(requested_tools)
    combined_tools = f"{base_tools},{added}" if base_tools else added
    logger.info("Tool request approved (scope=%s): tools=%s combined=%s thread=%s",
                scope, requested_tools, combined_tools, thread_ts)

    prompt = t(f"tool_request_approved_{scope}", tools=tools_display)
    if comment:
        prompt += "\n\n" + t("tool_request_user_comment", comment=comment)
    task = Task(
        id=0,
        prompt=prompt,
        allowed_tools=combined_tools,
        user_id=user_id,
    )
    task.resume_session = session.claude_session_id

    # 前タスクの bell を外して、新タスクのリアクション対象を引き継ぐ
    if prev_task:
        inherited_targets = list(prev_task.reaction_targets)
        runner._swap_task_reaction(prev_task, None)
    else:
        inherited_targets = []

    runner.assign_task_id(task)
    task.reaction_targets = inherited_targets
    runner._swap_task_reaction(task, REACTION_RECEIVED)

    err = runner.run_task(project, session, task)
    if err:
        logger.warning("Tool request approve: run_task error=%s thread=%s", err, thread_ts)
        try:
            slack_client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=err,
            )
        except Exception as e:
            logger.error("Tool request error post: %s", e)


@app.action("tool_request_approve_once")
def handle_tool_approve_once(ack, body):
    _handle_tool_request_action(ack, body, scope="once")


@app.action("tool_request_approve_session")
def handle_tool_approve_session(ack, body):
    _handle_tool_request_action(ack, body, scope="session")


@app.action("tool_request_approve_project")
def handle_tool_approve_project(ack, body):
    _handle_tool_request_action(ack, body, scope="project")


@app.action("tool_request_reject")
def handle_tool_reject(ack, body):
    _handle_tool_request_action(ack, body, scope="reject")


def _help_text() -> str:
    return t("help_text")


def _slack_thread_link(channel_id: str, thread_ts: str,
                       msg_ts: Optional[str] = None) -> str:
    """Slackスレッドへのリンクを生成。
    msg_ts を指定するとスレッド内の該当メッセージ（最新タスク等）へディープリンクする。"""
    if msg_ts and msg_ts != thread_ts:
        ts_no_dot = msg_ts.replace(".", "")
        return (f"https://app.slack.com/archives/{channel_id}/p{ts_no_dot}"
                f"?thread_ts={thread_ts}&cid={channel_id}")
    ts_no_dot = thread_ts.replace(".", "")
    return f"https://app.slack.com/archives/{channel_id}/p{ts_no_dot}"


# ─────────────────────────────────────────────────────────────────────────
# App Home ダッシュボード
#   - whitelist内全チャンネル + Mac上の外部Claudeを横断したセッション一覧
#   - 進行中(ブリッジ管理/外部) + 終了済み(session_history) を表示
#   - 「自分のみ / 全員」トグル、外部プロセスの fork ボタン
# ─────────────────────────────────────────────────────────────────────────

# 最近ホームを開いた viewer の状態: user_id -> {"last": datetime, "show_all": bool}
_home_viewers: dict[str, dict] = {}
_home_viewers_lock = threading.Lock()

_FINISHED_STATUS_EMOJI = {
    "completed": ":white_check_mark:",
    "failed": ":x:",
    "cancelled": ":stop_sign:",
}


def _dash_truncate(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def _fmt_duration(seconds: float) -> str:
    """経過秒数を簡潔な日本語/英語表記に整形。"""
    seconds = int(max(0, seconds))
    if seconds < 90:
        return t("home_dur_sec", n=seconds)
    mins = seconds // 60
    if mins < 90:
        return t("home_dur_min", n=mins)
    hours = mins // 60
    return t("home_dur_hour", n=hours, m=mins % 60)


def _fmt_completed_at(iso_str: Optional[str]) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return ""
    return dt.strftime("%m/%d %H:%M")


def _collect_dashboard_sessions() -> dict:
    """ダッシュボード表示用にセッション情報を横断収集。
    返す: {"running_bridge": [...], "running_external": [...], "finished": [...]}"""
    running_bridge: list[dict] = []
    active_thread_keys: set[tuple] = set()
    for channel_id, project in list(runner.projects.items()):
        for session in project.active_sessions:
            task = session.active_task
            if not task:
                continue
            active_thread_keys.add((channel_id, session.thread_ts))
            elapsed = (datetime.now() - task.started_at).total_seconds() if task.started_at else 0
            running_bridge.append({
                "channel_id": channel_id,
                "thread_ts": session.thread_ts,
                "anchor_ts": _task_anchor_ts(task),
                "label_emoji": session.label_emoji,
                "label_name": session.label_name,
                "working_dir": session.working_dir,
                "user_id": task.user_id,
                "prompt": task.prompt or "",
                "elapsed": elapsed,
                "tool_count": len(task.tool_calls),
                "recent_tools": [tc["name"] for tc in task.tool_calls[-3:]],
            })

    # 外部プロセス（ブリッジ管轄外で今生きているもの）
    running_external: list[dict] = []
    try:
        instances = detect_running_claude_instances()
    except Exception as e:
        logger.warning("dashboard: detect instances failed: %s", e)
        instances = []
    tracked_pids = {d.get("pid") for d in instance_threads.values() if d.get("pid")}
    for ext in instances:
        pid = ext.get("pid")
        if pid in tracked_pids:
            continue
        try:
            if _is_descendant_of_bridge(pid):
                continue
        except Exception:
            pass
        running_external.append({"pid": pid, "cwd": ext.get("cwd"), "etime": ext.get("etime")})

    # 終了済み（現在アクティブなスレッドは進行中側で表示するため除外）
    finished: list[dict] = []
    for entry in runner.get_session_history():
        key = (entry.get("channel_id"), entry.get("thread_ts"))
        if key in active_thread_keys:
            continue
        finished.append(entry)

    return {
        "running_bridge": running_bridge,
        "running_external": running_external,
        "finished": finished,
    }


def _dir_key(path: Optional[str]) -> Optional[str]:
    """working_dir / cwd を正規化したグルーピングキー。不明なら None。"""
    if not path:
        return None
    try:
        return os.path.normpath(os.path.expanduser(path))
    except (TypeError, ValueError):
        return None


def _iso_ts(iso_str: Optional[str]) -> float:
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _group_dashboard_by_dir(running_bridge: list, running_external: list,
                            finished: list) -> list:
    """進行中(ブリッジ/外部) + 終了済み を working_dir 単位に統合したグループ列を返す。
    並び: 進行中アイテムを含むグループ優先 → 直近アクティビティの降順。
    各グループ内: ブリッジ進行中 → 外部進行中 → 終了済み（各々 recency 降順）。"""
    now = datetime.now().timestamp()
    groups: dict[str, dict] = {}

    def grp(path: Optional[str]) -> dict:
        key = _dir_key(path) or "?"
        g = groups.get(key)
        if not g:
            g = {"key": key, "path": (key if key != "?" else None),
                 "bridge": [], "external": [], "finished": [], "recency": 0.0}
            groups[key] = g
        return g

    for s in running_bridge:
        rec = now - float(s.get("elapsed") or 0)
        s["_recency"] = rec
        g = grp(s.get("working_dir"))
        g["bridge"].append(s)
        g["recency"] = max(g["recency"], rec)
    for ext in running_external:
        ext["_recency"] = now
        g = grp(ext.get("cwd"))
        g["external"].append(ext)
        g["recency"] = max(g["recency"], now)
    for e in finished:
        rec = _iso_ts(e.get("completed_at"))
        e["_recency"] = rec
        g = grp(e.get("working_dir"))
        g["finished"].append(e)
        g["recency"] = max(g["recency"], rec)

    for g in groups.values():
        g["bridge"].sort(key=lambda x: x["_recency"], reverse=True)
        g["finished"].sort(key=lambda x: x["_recency"], reverse=True)
        g["has_running"] = bool(g["bridge"] or g["external"])

    return sorted(groups.values(),
                  key=lambda g: (g["has_running"], g["recency"]), reverse=True)


def _home_bridge_block(s: dict) -> dict:
    link = _slack_thread_link(s["channel_id"], s["thread_ts"], s.get("anchor_ts"))
    user_field = f"<@{s['user_id']}>" if s["user_id"] else "-"
    recent = " → ".join(f"`{n}`" for n in s["recent_tools"]) if s["recent_tools"] else "-"
    return {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f":gear: {s['label_emoji']} <{link}|{t('home_open_thread')}>  "
                         f"_{_dash_truncate(s['prompt'], 80)}_"},
        "fields": [
            {"type": "mrkdwn", "text": f"*User:* {user_field}"},
            {"type": "mrkdwn", "text": f"*Time:* {_fmt_duration(s['elapsed'])}"},
            {"type": "mrkdwn", "text": f"*Tools:* {s['tool_count']}  {recent}"},
        ],
    }


def _home_external_block(ext: dict) -> dict:
    cwd = ext.get("cwd") or "(unknown)"
    return {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": t("home_ext_line", pid=ext["pid"], etime=ext.get("etime") or "?")},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": t("home_fork_button"), "emoji": True},
            "action_id": "home_fork",
            "value": json.dumps({"pid": ext["pid"], "cwd": cwd}),
        },
    }


def _home_finished_block(e: dict) -> dict:
    emoji = _FINISHED_STATUS_EMOJI.get(e.get("latest_status"), ":grey_question:")
    link = _slack_thread_link(e["channel_id"], e["thread_ts"], e.get("latest_msg_ts"))
    when = _fmt_completed_at(e.get("completed_at"))
    label = e.get("label_emoji") or ""
    return {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"{emoji} {label} <{link}|{t('home_open_thread')}>  "
                         f"_{_dash_truncate(e.get('latest_prompt', ''), 60)}_ · {when}"},
    }


def _build_home_view(viewer_user_id: str, show_all: bool) -> dict:
    """Block Kit の Home ビューを構築。show_all=False なら viewer 本人のセッションのみ。"""
    data = _collect_dashboard_sessions()
    running_bridge = data["running_bridge"]
    running_external = data["running_external"]
    finished = data["finished"]

    if not show_all:
        running_bridge = [s for s in running_bridge if s["user_id"] == viewer_user_id]
        finished = [s for s in finished if s.get("user_id") == viewer_user_id]
    # 外部プロセスは所有者を特定できないため常に表示

    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": t("home_title"), "emoji": True},
    })
    scope_label = t("home_scope_all") if show_all else t("home_scope_mine")
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": t("home_subtitle", scope=scope_label,
                                updated=datetime.now().strftime("%H:%M:%S"))}],
    })
    toggle_label = t("home_toggle_to_mine") if show_all else t("home_toggle_to_all")
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button",
             "text": {"type": "plain_text", "text": toggle_label, "emoji": True},
             "action_id": "home_toggle_scope",
             "value": "mine" if show_all else "all"},
            {"type": "button",
             "text": {"type": "plain_text", "text": t("home_refresh_button"), "emoji": True},
             "action_id": "home_refresh"},
        ],
    })
    blocks.append({"type": "divider"})

    # ── ワーキングディレクトリ単位でグループ化して表示 ──
    groups = _group_dashboard_by_dir(running_bridge, running_external, finished)
    total_running = len(running_bridge) + len(running_external)
    total_finished = len(finished)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": t("home_overview", groups=len(groups),
                  running=total_running, finished=total_finished)}]})

    if not groups:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": t("home_none")}]})

    # Slack の Home ビューは最大100ブロック。進行中は全件、終了済みは合計
    # SESSION_HISTORY_DISPLAY 件まで表示し、超過分は省略する。
    BLOCK_LIMIT = 96
    finished_budget = SESSION_HISTORY_DISPLAY
    shown_finished = 0
    truncated = False
    for g in groups:
        if len(blocks) >= BLOCK_LIMIT:
            truncated = True
            break
        blocks.append({"type": "divider"})
        running_n = len(g["bridge"]) + len(g["external"])
        name = (os.path.basename(g["key"]) or g["key"]) if g["path"] else t("home_group_unknown")
        summary = t("home_group_summary", running=running_n, finished=len(g["finished"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f":file_folder: *{name}*  {summary}"}})
        if g["path"]:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"`{g['path']}`"}]})
        for s in g["bridge"]:
            if len(blocks) >= BLOCK_LIMIT:
                truncated = True
                break
            blocks.append(_home_bridge_block(s))
        if truncated:
            break
        for ext in g["external"]:
            if len(blocks) >= BLOCK_LIMIT:
                truncated = True
                break
            blocks.append(_home_external_block(ext))
        if truncated:
            break
        for e in g["finished"]:
            if shown_finished >= finished_budget:
                break
            if len(blocks) >= BLOCK_LIMIT:
                truncated = True
                break
            blocks.append(_home_finished_block(e))
            shown_finished += 1
        if truncated:
            break

    remaining_finished = total_finished - shown_finished
    if remaining_finished > 0:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": t("home_more", count=remaining_finished)}]})
    if truncated:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": t("home_truncated")}]})

    # Slack の Home ビューは最大100ブロック。安全側で丸める。
    if len(blocks) > 98:
        blocks = blocks[:97]
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": t("home_truncated")}]})

    return {"type": "home", "blocks": blocks}


def _build_home_denied_view() -> dict:
    return {"type": "home", "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": t("home_not_allowed")}},
    ]}


# ── viewer 登録 / publish / 更新 ──
def _get_viewer_scope(user_id: str) -> bool:
    with _home_viewers_lock:
        v = _home_viewers.get(user_id)
        return bool(v and v.get("show_all"))


def _note_home_viewer(user_id: str, show_all: bool):
    with _home_viewers_lock:
        _home_viewers[user_id] = {"last": datetime.now(), "show_all": show_all}


def _publish_home(user_id: str, show_all: bool = False):
    try:
        view = _build_home_view(user_id, show_all)
        slack_client.views_publish(user_id=user_id, view=view)
    except Exception as e:
        logger.warning("views_publish failed user=%s: %s", user_id, e)


def _refresh_home_viewers():
    """最近ホームを開いた viewer 全員に再 publish。"""
    now = datetime.now()
    with _home_viewers_lock:
        viewers = [(uid, v.get("show_all", False))
                   for uid, v in _home_viewers.items()
                   if (now - v["last"]).total_seconds() <= APP_HOME_VIEWER_TTL]
    for uid, show_all in viewers:
        _publish_home(uid, show_all)


def _schedule_home_refresh():
    """進行中の変化を反映するため近接 viewer を非同期で再 publish（即時更新フック）。"""
    threading.Thread(target=_refresh_home_viewers, daemon=True).start()


def _has_running_sessions() -> bool:
    for project in list(runner.projects.values()):
        if project.active_sessions:
            return True
    return False


def _app_home_poll_loop():
    """進行中セッションがある間だけ定期的に viewer を再 publish。"""
    while True:
        try:
            time.sleep(APP_HOME_POLL_INTERVAL)
            if _has_running_sessions():
                _refresh_home_viewers()
        except Exception as e:
            logger.warning("app_home poll loop error: %s", e)


# ── 外部プロセスの fork（App Home から） ──
def _open_im(user_id: str) -> Optional[str]:
    try:
        resp = slack_client.conversations_open(users=user_id)
        return resp["channel"]["id"]
    except Exception as e:
        logger.warning("conversations_open failed user=%s: %s", user_id, e)
        return None


def _dm_user(user_id: str, text: str, blocks: Optional[list] = None):
    ch = _open_im(user_id)
    if not ch:
        return
    try:
        slack_client.chat_postMessage(channel=ch, text=text, blocks=blocks or None)
    except Exception as e:
        logger.warning("dm user failed user=%s: %s", user_id, e)


def _remove_action_buttons(body: dict, text: str):
    """ボタン押下元メッセージ（DM等）からボタンを除去。"""
    try:
        ch = body.get("channel", {}).get("id")
        ts = body.get("message", {}).get("ts")
        if ch and ts:
            slack_client.chat_update(channel=ch, ts=ts, text=text, blocks=[])
    except Exception as e:
        logger.warning("remove action buttons failed: %s", e)


def _find_channel_for_cwd(cwd: str) -> Optional[str]:
    """working_dir(=cwd) が一致するチャンネルを探す。channel root 優先、なければ既存セッション。"""
    target = os.path.normpath(cwd)
    try:
        roots = runner.load_channel_roots()
    except Exception:
        roots = {}
    for cid, root in roots.items():
        if root and os.path.normpath(root) == target:
            return cid
    for cid, project in list(runner.projects.items()):
        for s in project.sessions.values():
            if s.working_dir and os.path.normpath(s.working_dir) == target:
                return cid
    return None


def _sanitize_channel_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:80] or "claude-session"


def _create_channel_for_cwd(cwd: str, user_id: str) -> Optional[str]:
    """cwd 用の新規チャンネルを作成し、root を cwd に設定して channel_id を返す。"""
    base = os.path.basename(os.path.normpath(cwd)) or "session"
    name = _sanitize_channel_name(f"claude-{base}")
    try:
        resp = slack_client.conversations_create(name=name)
        channel_id = resp["channel"]["id"]
    except Exception as e:
        logger.warning("conversations_create failed name=%s: %s", name, e)
        return None
    try:
        slack_client.conversations_invite(channel=channel_id, users=user_id)
    except Exception as e:
        logger.info("conversations_invite skipped/failed channel=%s: %s", channel_id, e)
    # cwd をチャンネル root に設定して永続化
    try:
        project = runner.get_or_create_project(channel_id)
        project.root_dir = cwd
        runner.save_channel_roots()
    except Exception as e:
        logger.warning("set channel root failed channel=%s: %s", channel_id, e)
    return channel_id


def _fork_into_channel(pid: int, cwd: str, channel_id: str, user_id: str):
    """指定チャンネルに新規スレッドを作って外部プロセスを取り込む（pending_fork で待機）。"""
    try:
        parent = slack_client.chat_postMessage(
            channel=channel_id, text=t("home_fork_thread_header", cwd=cwd))
    except Exception as e:
        logger.warning("fork into channel post failed channel=%s: %s", channel_id, e)
        _dm_user(user_id, t("home_fork_failed"))
        return
    thread_ts = parent["ts"]

    def _say(**kwargs):
        kwargs.setdefault("channel", channel_id)
        try:
            return slack_client.chat_postMessage(**kwargs)
        except Exception as e:
            logger.warning("fork say failed: %s", e)

    _execute_fork({"pid": pid, "cwd": cwd}, channel_id, _say, thread_ts, user_id,
                  initial_input=None)
    _schedule_home_refresh()


def _home_fork_external(pid: int, cwd: str, user_id: str):
    if not _is_process_alive(pid):
        _dm_user(user_id, t("fork_pid_exited", pid=pid))
        _publish_home(user_id, _get_viewer_scope(user_id))
        return
    channel_id = _find_channel_for_cwd(cwd)
    if channel_id:
        _fork_into_channel(pid, cwd, channel_id, user_id)
        return
    # 一致するチャンネルがない → 「作成 / DM」の2択を DM で提示
    val = json.dumps({"pid": pid, "cwd": cwd})
    blocks = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": t("home_fork_no_channel", cwd=cwd)}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary",
             "text": {"type": "plain_text", "text": t("home_fork_create_channel"), "emoji": True},
             "action_id": "home_fork_create_channel", "value": val},
            {"type": "button",
             "text": {"type": "plain_text", "text": t("home_fork_use_dm"), "emoji": True},
             "action_id": "home_fork_use_dm", "value": val},
        ]},
    ]
    _dm_user(user_id, t("home_fork_no_channel", cwd=cwd), blocks)


# ── App Home イベント / アクション ハンドラ ──
@app.event("app_home_opened")
def handle_app_home_opened(event):
    if event.get("tab") != "home":
        return
    user_id = event.get("user")
    if not user_id:
        return
    if not _is_user_allowed(user_id):
        try:
            slack_client.views_publish(user_id=user_id, view=_build_home_denied_view())
        except Exception:
            pass
        return
    show_all = _get_viewer_scope(user_id)
    _note_home_viewer(user_id, show_all)
    _publish_home(user_id, show_all)


@app.action("home_toggle_scope")
def handle_home_toggle_scope(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if not user_id:
        return
    new_scope = body["actions"][0].get("value") == "all"
    _note_home_viewer(user_id, new_scope)
    _publish_home(user_id, new_scope)


@app.action("home_refresh")
def handle_home_refresh(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if not user_id:
        return
    show_all = _get_viewer_scope(user_id)
    _note_home_viewer(user_id, show_all)
    _publish_home(user_id, show_all)


@app.action("home_fork")
def handle_home_fork(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if not user_id or not _is_user_allowed(user_id):
        return
    try:
        val = json.loads(body["actions"][0]["value"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return
    _home_fork_external(val.get("pid"), val.get("cwd"), user_id)


@app.action("home_fork_create_channel")
def handle_home_fork_create_channel(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if not user_id or not _is_user_allowed(user_id):
        return
    try:
        val = json.loads(body["actions"][0]["value"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return
    pid, cwd = val.get("pid"), val.get("cwd")
    _remove_action_buttons(body, t("home_fork_creating"))
    channel_id = _create_channel_for_cwd(cwd, user_id)
    if not channel_id:
        # 作成失敗 → DM へフォールバック
        im = _open_im(user_id)
        if im:
            _fork_into_channel(pid, cwd, im, user_id)
        return
    _fork_into_channel(pid, cwd, channel_id, user_id)


@app.action("home_fork_use_dm")
def handle_home_fork_use_dm(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if not user_id or not _is_user_allowed(user_id):
        return
    try:
        val = json.loads(body["actions"][0]["value"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return
    pid, cwd = val.get("pid"), val.get("cwd")
    _remove_action_buttons(body, t("home_fork_opening_dm"))
    im = _open_im(user_id)
    if im:
        _fork_into_channel(pid, cwd, im, user_id)


def _handle_status(say, thread_ts, channel_id: str):
    """プロジェクトスコープのタスク状態一覧（Block Kit）"""
    project = runner.get_project(channel_id)
    if not project:
        say(text=t("status_no_tasks"), thread_ts=thread_ts)
        return

    active_sessions = project.active_sessions
    if not active_sessions:
        # 直近の完了タスクをBlock Kitで表示
        task_session_map: dict[int, Session] = {}
        for session in project.sessions.values():
            for tk in session.tasks:
                task_session_map[tk.id] = session
        all_tasks = project.all_tasks
        recent = [tk for tk in all_tasks if tk.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)][-5:]
        if recent:
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": t("status_no_running_with_recent")}}]
            for tk in reversed(recent):
                emoji = ":white_check_mark:" if tk.status == TaskStatus.COMPLETED else ":x:"
                elapsed_str = ""
                if tk.started_at and tk.completed_at:
                    elapsed_str = " " + t("status_elapsed_seconds", elapsed=(tk.completed_at - tk.started_at).total_seconds())
                session = task_session_map.get(tk.id)
                thread_link = ""
                if session:
                    link_url = _slack_thread_link(channel_id, session.thread_ts)
                    thread_link = f"  <{link_url}|:speech_balloon:>"
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{emoji} {tk.short_id} {tk.prompt[:40]}{elapsed_str}{thread_link}"},
                })
            fallback = t("status_no_running_with_recent")
            say(text=fallback, blocks=blocks, thread_ts=thread_ts)
        else:
            say(text=t("status_no_running_tasks"), thread_ts=thread_ts)
        return

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": t("status_running_tasks_header", count=len(active_sessions))}}]
    for session in active_sessions:
        task = session.active_task
        if not task:
            continue
        elapsed = (datetime.now() - task.started_at).total_seconds() if task.started_at else 0
        prompt_preview = task.prompt[:50] + ("..." if len(task.prompt) > 50 else "")
        tool_count = len(task.tool_calls)
        thread_link_url = _slack_thread_link(channel_id, session.thread_ts)

        # カード風のセクションブロック（fieldsで構造化表示）
        dir_name = os.path.basename(session.working_dir) if session.working_dir else "?"
        user_field = f"<@{task.user_id}>" if task.user_id else "-"
        recent_tools = ""
        if task.tool_calls:
            recent_tools = " → ".join(f"`{tc['name']}`" for tc in task.tool_calls[-3:])

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{session.label_emoji} {task.short_id}  <{thread_link_url}|:speech_balloon:>\n> {prompt_preview}",
            },
            "fields": [
                {"type": "mrkdwn", "text": f"*Dir:* `{dir_name}`"},
                {"type": "mrkdwn", "text": f"*User:* {user_field}"},
                {"type": "mrkdwn", "text": f"*Time:* {elapsed:.0f}s"},
                {"type": "mrkdwn", "text": f"*Tools:* {tool_count}"},
            ],
        })
        if recent_tools:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": t("status_recent_tools", tools=recent_tools)}],
            })
        if session.pending_followup:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": t("status_followup_queued", count=len(session.pending_followup))}],
            })

    fallback = t("status_running_tasks_header", count=len(active_sessions))
    say(text=fallback, blocks=blocks, thread_ts=thread_ts)


def _handle_cancel(text: str, say, thread_ts, channel_id: str):
    """トップレベル cancel — プロジェクト内のアクティブタスクが1つならそれをキャンセル"""
    task_id = None
    project = runner.get_project(channel_id)
    if project:
        active = project.active_sessions
        if len(active) == 1 and active[0].active_task:
            task_id = active[0].active_task.id
    if task_id is None:
        say(
            text=t("error_cancel_no_active"),
            thread_ts=thread_ts,
        )
        return

    if runner.cancel_task(task_id):
        say(text=t("task_cancel_request_sent", task_id=task_id), thread_ts=thread_ts)
    else:
        say(text=t("task_not_running", task_id=task_id), thread_ts=thread_ts)


def _handle_cancel_in_thread(say, thread_ts: str, channel_id: str):
    """スレッド内 cancel — そのスレッドのセッションのアクティブタスクをキャンセル"""
    project = runner.get_project(channel_id)
    if not project:
        say(text=t("error_cancel_no_active"), thread_ts=thread_ts)
        return
    session = project.sessions.get(thread_ts)
    if not session or not session.active_task or session.active_task.status != TaskStatus.RUNNING:
        say(text=t("error_cancel_no_active"), thread_ts=thread_ts)
        return
    # 引数なし cancel はキュー済みの追加指示も破棄（中断のみで終わらせる）
    with runner.lock:
        dropped = len(session.pending_followup)
        session.pending_followup.clear()
    task_id = session.active_task.id
    if runner.cancel_task(task_id):
        msg = t("task_cancel_request_sent", task_id=task_id)
        if dropped:
            msg += " " + t("followup_dropped", count=dropped)
        say(text=msg, thread_ts=thread_ts)
    else:
        say(text=t("task_not_running", task_id=task_id), thread_ts=thread_ts)


def _handle_team(rest: str, say, thread_ts: str, channel_id: str,
                  user_id: str = "", files: list[dict] | None = None,
                  request_msg_ts: str | None = None):
    """team [in <path>] <タスク> — Team Agentモードでタスク実行。
    プロンプトにチーム指示を注入し、allowedToolsにTeam系ツールを追加する。"""
    if not rest:
        say(text=t("team_usage"), thread_ts=thread_ts)
        return

    # "team in <path> <task>" 形式のパース
    rest_lower = rest.lower()
    if rest_lower.startswith("in "):
        inner = rest[3:].strip()
        parts = inner.split(maxsplit=1)
        if len(parts) < 2:
            say(text=t("team_usage"), thread_ts=thread_ts)
            return
        dir_path, prompt = parts
        dir_path = os.path.expanduser(dir_path)
        if not os.path.isabs(dir_path):
            root = runner.get_channel_root(channel_id)
            if root:
                dir_path = os.path.normpath(os.path.join(root, dir_path))
            else:
                say(text=t("error_absolute_path_with_root_hint", path=dir_path), thread_ts=thread_ts)
                return
    else:
        # "team <task>" — root設定が必要
        root = runner.get_channel_root(channel_id)
        if not root:
            say(text=t("team_usage"), thread_ts=thread_ts)
            return
        dir_path = root
        prompt = rest

    if not os.path.isdir(dir_path):
        say(text=t("error_dir_not_found", path=dir_path), thread_ts=thread_ts)
        return

    # プロンプトにTeam Agent指示を注入
    team_prompt = t("team_prompt_prefix") + prompt

    # Team系ツールを追加した allowedTools でタスク起動
    project = runner.get_or_create_project(channel_id)
    session = project.get_or_create_session(thread_ts)
    session.working_dir = dir_path
    runner.record_directory(channel_id, dir_path)

    if files:
        file_paths = _download_slack_files(files, dir_path)
        if file_paths:
            team_prompt = _augment_prompt_with_files(team_prompt, file_paths)

    # 既存ツールにTeam系ツールをマージ
    base_tools = session.consume_tools() or DEFAULT_ALLOWED_TOOLS
    base_set = set(t_.strip() for t_ in base_tools.split(",")) if base_tools else set()
    team_set = set(t_.strip() for t_ in TEAM_EXTRA_TOOLS.split(","))
    merged_tools = ",".join(sorted(base_set | team_set))

    task = Task(
        id=0,
        prompt=team_prompt,
        allowed_tools=merged_tools,
        user_id=user_id,
    )
    runner.assign_task_id(task)
    task.reaction_targets = _build_reaction_targets(
        channel_id, request_msg_ts, thread_ts,
    )
    runner._swap_task_reaction(task, REACTION_RECEIVED)

    err = runner.run_task(project, session, task)
    if err:
        say(text=err, thread_ts=thread_ts)


def _handle_root(text: str, say, thread_ts: str, channel_id: str):
    """root [<path>|clear] — チャンネルのルートディレクトリを設定/表示/解除"""
    rest = text[4:].strip()

    project = runner.get_or_create_project(channel_id)

    if not rest:
        # 現在のルートを表示
        if project.root_dir:
            say(text=t("root_current", path=project.root_dir), thread_ts=thread_ts)
        else:
            say(text=t("root_not_set"), thread_ts=thread_ts)
        return

    if rest == "clear":
        # ルートを解除
        if project.root_dir:
            old = project.root_dir
            project.root_dir = None
            runner.save_channel_roots()
            say(text=t("root_cleared", old=old), thread_ts=thread_ts)
        else:
            say(text=t("root_already_not_set"), thread_ts=thread_ts)
        return

    # パスを設定
    dir_path = os.path.expanduser(rest)

    if not os.path.isabs(dir_path):
        say(text=t("error_absolute_path_required", path=dir_path), thread_ts=thread_ts)
        return

    if not os.path.isdir(dir_path):
        say(text=t("error_dir_not_found", path=dir_path), thread_ts=thread_ts)
        return

    project.root_dir = dir_path
    runner.save_channel_roots()
    say(text=t("root_set", path=dir_path))


def _start_task_in_dir(dir_path: str, prompt: str, say, thread_ts: str,
                       channel_id: str, user_id: str = "",
                       files: list[dict] | None = None,
                       request_msg_ts: str | None = None):
    """指定ディレクトリでProject取得→Session作成→Task実行の共通ヘルパー"""
    project = runner.get_or_create_project(channel_id)
    session = project.get_or_create_session(thread_ts)
    session.working_dir = dir_path
    runner.record_directory(channel_id, dir_path)

    # 添付ファイルのダウンロードとプロンプト拡張
    if files:
        file_paths = _download_slack_files(files, dir_path)
        if file_paths:
            prompt = _augment_prompt_with_files(prompt, file_paths)

    task = Task(
        id=0,
        prompt=prompt,
        allowed_tools=session.consume_tools(),
        user_id=user_id,
    )
    # 受付リアクション
    runner.assign_task_id(task)
    task.reaction_targets = _build_reaction_targets(
        channel_id, request_msg_ts, thread_ts,
    )
    runner._swap_task_reaction(task, REACTION_RECEIVED)

    err = runner.run_task(project, session, task)
    if err:
        say(text=err, thread_ts=thread_ts)


def _handle_in_dir(text: str, say, thread_ts, channel_id: str, user_id: str = "",
                   files: list[dict] | None = None,
                   request_msg_ts: str | None = None):
    """in <path> <task> — 指定ディレクトリで即座に実行"""
    rest = text[3:].strip()
    parts = rest.split(maxsplit=1)
    if len(parts) < 2:
        say(text=t("error_in_usage"), thread_ts=thread_ts)
        return

    dir_path, prompt = parts
    dir_path = os.path.expanduser(dir_path)

    if not os.path.isabs(dir_path):
        # ルートディレクトリが設定されていれば相対パスを解決
        root = runner.get_channel_root(channel_id)
        if root:
            dir_path = os.path.normpath(os.path.join(root, dir_path))
        else:
            say(
                text=t("error_absolute_path_with_root_hint", path=dir_path),
                thread_ts=thread_ts,
            )
            return

    if not os.path.isdir(dir_path):
        say(text=t("error_dir_not_found", path=dir_path), thread_ts=thread_ts)
        return

    _start_task_in_dir(dir_path, prompt, say, thread_ts, channel_id, user_id, files,
                       request_msg_ts=request_msg_ts)


def _parse_thread_link(text: str) -> tuple[str | None, str | None]:
    """Slackスレッドリンクからchannel_id, thread_tsを抽出。
    例: https://app.slack.com/archives/C12345/p1234567890123456"""
    m = re.search(r"archives/([A-Z0-9]+)/p(\d+)", text)
    if m:
        channel = m.group(1)
        raw_ts = m.group(2)
        # p1234567890123456 → 1234567890.123456
        thread_ts = raw_ts[:-6] + "." + raw_ts[-6:]
        return channel, thread_ts
    return None, None


def _search_forkable_sessions(query: str, channel_id: str) -> list[dict]:
    """クエリ文字列でブリッジセッションを絞り込み検索。
    スレッドリンク、ラベル（部分一致）、セッションID（前方一致）に対応。"""
    # スレッドリンク
    link_ch, link_ts = _parse_thread_link(query)
    if link_ch and link_ts:
        # リンク先のチャンネルのセッションを検索
        project = runner.get_project(link_ch)
        if project and link_ts in project.sessions:
            s = project.sessions[link_ts]
            if s.claude_session_id and s.working_dir:
                return [{"session": s, "channel_id": link_ch}]
        return []

    # ラベル・セッションID 検索（現チャンネルのみ）
    all_sessions = _collect_forkable_sessions(channel_id)
    results = []
    query_lower = query.lower()
    for item in all_sessions:
        s = item["session"]
        # ラベル部分一致
        if s.label_name and query_lower in s.label_name.lower():
            results.append(item)
        # セッションID前方一致
        elif s.claude_session_id and s.claude_session_id.startswith(query):
            results.append(item)
    return results


def _handle_fork(rest: str, say, thread_ts, channel_id: str, user_id: str,
                 files: list[dict] | None = None,
                 request_msg_ts: str | None = None):
    """fork <PID|スレッドリンク|ラベル|セッションID> [<task>] — セッションをフォーク"""
    parts = rest.split(maxsplit=1)
    identifier = parts[0]
    initial_input = parts[1] if len(parts) > 1 else None

    # PID指定を試行
    try:
        target_pid = int(identifier)
        # 数値 → 外部プロセスfork（従来動作）
        instances = detect_running_claude_instances()
        if not instances:
            say(text=t("fork_no_instances"), thread_ts=thread_ts)
            return

        tracked_pids = {data["pid"] for data in instance_threads.values()}
        candidates = [i for i in instances if i["pid"] not in tracked_pids]

        matched = [i for i in candidates if i["pid"] == target_pid]
        if not matched:
            if target_pid in tracked_pids:
                say(text=t("fork_pid_already_tracked", pid=target_pid), thread_ts=thread_ts)
            else:
                say(text=t("fork_pid_not_found", pid=target_pid), thread_ts=thread_ts)
            return

        _execute_fork(matched[0], channel_id, say, thread_ts, user_id, initial_input,
                      request_msg_ts=request_msg_ts)
        return
    except ValueError:
        pass

    # スレッドリンク / ラベル / セッションID → ブリッジセッションfork
    # identifier にスペースを含むラベルの場合を考慮し、restをスレッドリンク検出でも試す
    link_ch, link_ts = _parse_thread_link(rest)
    if link_ch:
        # スレッドリンク + 残りをタスクとして扱う
        # Slackは <URL> や <URL|label> 形式でリンクを送るので、その全体を除去
        remainder = re.sub(r"<[^>]*>", "", rest).strip()
        initial_input = remainder if remainder else None
        matches = _search_forkable_sessions(rest, channel_id)
    else:
        matches = _search_forkable_sessions(identifier, channel_id)

    if not matches:
        say(text=t("fork_session_not_found", query=identifier), thread_ts=thread_ts)
        return

    if len(matches) == 1:
        # 一意にマッチ → 即実行
        item = matches[0]
        s = item["session"]
        _execute_fork_session(
            source_session_id=s.claude_session_id,
            cwd=s.working_dir,
            channel_id=channel_id,
            say=say,
            thread_ts=thread_ts,
            user_id=user_id,
            initial_input=initial_input,
            source_label=s.display_label or s.claude_session_id[:12],
            request_msg_ts=request_msg_ts,
        )
        return

    # 複数マッチ → 選択肢表示
    options = []
    lines = [t("fork_session_matches", query=identifier)]
    for idx, item in enumerate(matches, 1):
        s = item["session"]
        desc = _format_session_option(s, item["channel_id"])
        lines.append(f"  `{idx}` — {desc}")
        options.append(("session", item))
    lines.append(t("fork_select_or_cancel"))
    say(text="\n".join(lines), thread_ts=thread_ts)

    pending_fork_selections[channel_id] = {
        "options": options,
        "thread_ts": thread_ts,
        "user_id": user_id,
        "initial_input": initial_input,
        "request_msg_ts": request_msg_ts,
    }


def _handle_sessions(say, thread_ts, channel_id: str):
    """プロジェクトスコープのセッション一覧を表示"""
    project = runner.get_project(channel_id)
    if not project or not project.sessions:
        say(text=t("session_no_history"), thread_ts=thread_ts)
        return

    lines = [t("session_list_header")]

    # 最新のセッション10件を表示
    sessions = sorted(
        project.sessions.values(),
        key=lambda s: s.created_at or datetime.min,
        reverse=True,
    )[:10]

    for session in sessions:
        active = session.active_task
        if active:
            status_emoji = ":gear:"
        elif session.latest_task:
            status_emoji = {
                TaskStatus.COMPLETED: ":white_check_mark:",
                TaskStatus.FAILED: ":x:",
                TaskStatus.CANCELLED: ":stop_sign:",
            }.get(session.latest_task.status, ":grey_question:")
        else:
            status_emoji = ":grey_question:"

        sid = session.claude_session_id[:16] + "..." if session.claude_session_id else "N/A"
        task_count = len(session.tasks)
        cwd = session.working_dir or ""
        dir_name = os.path.basename(cwd) if cwd else "N/A"
        latest = session.latest_task
        prompt_preview = latest.prompt[:40] + ("..." if latest and len(latest.prompt) > 40 else "") if latest else ""

        thread_link_url = _slack_thread_link(channel_id, session.thread_ts)
        thread_link = f"<{thread_link_url}|:speech_balloon:>"
        lines.append(
            f"{status_emoji} {session.label_emoji} `{sid}` {t('session_task_count', count=task_count)}"
            f" :file_folder:`{dir_name}` {prompt_preview} {thread_link}"
        )

    lines.append(t("session_reply_to_continue"))
    say(text="\n".join(lines), thread_ts=thread_ts)


def _collect_forkable_sessions(channel_id: str) -> list[dict]:
    """ブリッジ管理セッションからフォーク可能なもの（claude_session_id あり）を収集。
    返す各要素: {"session": Session, "channel_id": str}"""
    results = []
    project = runner.get_project(channel_id)
    if not project:
        return results
    for session in project.sessions.values():
        if session.claude_session_id and session.working_dir:
            results.append({"session": session, "channel_id": channel_id})
    # 最新順
    results.sort(key=lambda x: x["session"].created_at or datetime.min, reverse=True)
    return results


def _format_session_option(session: Session, channel_id: str) -> str:
    """セッション選択肢の表示文字列を生成。
    初回と最新のユーザー指示の冒頭を表示して内容がわかるようにする。"""
    cwd = session.working_dir or ""
    dir_name = os.path.basename(cwd) if cwd else "N/A"
    thread_link_url = _slack_thread_link(channel_id, session.thread_ts)
    thread_link = f"<{thread_link_url}|:speech_balloon:>"

    prompts = session.prompts
    first = prompts[0] if prompts else None
    latest = prompts[-1] if len(prompts) >= 2 else None

    def _preview(text: str, max_len: int = 30) -> str:
        return text[:max_len] + ("..." if len(text) > max_len else "")

    parts = [f":file_folder:`{dir_name}`"]
    if first:
        parts.append(f"_{_preview(first)}_")
    if latest:
        parts.append(f":arrow_right: _{_preview(latest)}_")
    parts.append(thread_link)
    return " ".join(parts)


def _handle_fork_list(say, thread_ts, channel_id: str, user_id: str):
    """fork（引数なし）: 外部プロセス + ブリッジセッション両方のフォーク候補を表示"""
    # 外部プロセス
    instances = detect_running_claude_instances()
    tracked_pids = {data["pid"] for data in instance_threads.values()}
    ext_candidates = [i for i in instances if i["pid"] not in tracked_pids] if instances else []

    # ブリッジセッション
    bridge_sessions = _collect_forkable_sessions(channel_id)

    if not ext_candidates and not bridge_sessions:
        say(text=t("fork_no_forkable"), thread_ts=thread_ts)
        return

    options = []  # (type, data)
    lines = [t("fork_list_header")]
    idx = 1

    # 外部プロセス
    if ext_candidates:
        lines.append(t("fork_ext_header"))
        for inst in ext_candidates:
            lines.append(f"  `{idx}` — PID {inst['pid']}  :file_folder: `{inst['cwd']}`  :clock1: {inst['etime']}")
            options.append(("ext", inst))
            idx += 1

    # ブリッジセッション
    if bridge_sessions:
        lines.append(t("fork_session_header"))
        for item in bridge_sessions:
            s = item["session"]
            desc = _format_session_option(s, item["channel_id"])
            lines.append(f"  `{idx}` — {desc}")
            options.append(("session", item))
            idx += 1

    lines.append(t("fork_select_or_cancel"))
    say(text="\n".join(lines), thread_ts=thread_ts)

    pending_fork_selections[channel_id] = {
        "options": options,
        "thread_ts": thread_ts,
        "user_id": user_id,
    }


def _execute_fork_session(source_session_id: str, cwd: str, channel_id: str,
                          say, thread_ts: str, user_id: str,
                          initial_input: str | None = None,
                          source_label: str = "",
                          request_msg_ts: str | None = None):
    """セッションIDからフォーク実行（共通処理）。
    initial_input がある場合: 即座に --resume --fork-session でCLIを起動。
    initial_input がない場合: セッション登録のみ（pending_fork=True）。
      ユーザーがスレッド返信したときに初めて --fork-session 付きで起動。"""

    # Project / Session 作成
    project = runner.get_or_create_project(channel_id)
    session = project.get_or_create_session(thread_ts)
    session.working_dir = cwd
    session.claude_session_id = source_session_id
    runner.save_sessions()
    runner.record_directory(channel_id, cwd)

    # フォーク通知
    sid_short = source_session_id[:12] + "..."
    say(
        text=t("fork_session_start", source=source_label or sid_short, cwd=cwd),
        thread_ts=thread_ts,
    )

    if not initial_input:
        # タスク指示なし → セッション登録のみ。次のスレッド返信で --fork-session 付き起動
        session.pending_fork = True
        runner.save_sessions()
        return

    # --resume --fork-session でタスク即時起動
    task = Task(
        id=0,
        prompt=initial_input,
        allowed_tools=session.consume_tools(),
        user_id=user_id,
    )
    task.resume_session = source_session_id
    task.fork_session = True

    runner.assign_task_id(task)
    task.reaction_targets = _build_reaction_targets(
        channel_id, request_msg_ts, thread_ts,
    )
    runner._swap_task_reaction(task, REACTION_RECEIVED)

    err = runner.run_task(project, session, task)
    if err:
        say(text=err, thread_ts=thread_ts)


def _execute_fork(inst: dict, channel_id: str, say, thread_ts, user_id: str,
                  initial_input: str | None = None,
                  request_msg_ts: str | None = None):
    """外部プロセスからのフォーク実行: session_id を取得して --fork-session で分岐"""
    pid = inst["pid"]
    cwd = inst["cwd"]

    # JSONL から session_id を抽出
    jsonl_path = _find_session_jsonl(cwd)
    session_id = None
    if jsonl_path:
        dummy_task = Task(id=0, prompt="(fork)")
        dummy_session = type("_S", (), {"claude_session_id": None})()
        _extract_session_info_from_jsonl(dummy_task, dummy_session, jsonl_path)
        session_id = dummy_session.claude_session_id

    if not session_id:
        say(
            text=t("fork_session_id_not_found", pid=pid, cwd=cwd),
            thread_ts=thread_ts,
        )
        return

    _execute_fork_session(
        source_session_id=session_id,
        cwd=cwd,
        channel_id=channel_id,
        say=say,
        thread_ts=thread_ts,
        user_id=user_id,
        initial_input=initial_input,
        source_label=f"PID {pid}",
        request_msg_ts=request_msg_ts,
    )


def _handle_fork_selection(text: str, say, channel_id: str) -> bool:
    """pending_fork_selections の番号選択/キャンセルを処理。
    処理した場合 True、fallthrough の場合 False を返す。"""
    if channel_id not in pending_fork_selections:
        return False

    selection = pending_fork_selections[channel_id]
    options = selection["options"]
    thread_ts = selection["thread_ts"]
    user_id = selection["user_id"]
    initial_input = selection.get("initial_input")
    request_msg_ts = selection.get("request_msg_ts")
    input_text = text.strip().lower()

    # cancel
    if input_text == "cancel":
        del pending_fork_selections[channel_id]
        say(text=t("fork_cancelled"), thread_ts=thread_ts)
        return True

    # 番号入力
    try:
        num = int(input_text)
    except ValueError:
        return False  # 数字でもcancelでもない → 通常のコマンド処理にfallthrough

    if num < 1 or num > len(options):
        say(text=t("error_enter_number_range", max=len(options)), thread_ts=thread_ts)
        return True

    opt_type, opt_data = options[num - 1]
    del pending_fork_selections[channel_id]

    if opt_type == "ext":
        # 外部プロセス
        if not _is_process_alive(opt_data["pid"]):
            say(text=t("fork_pid_exited", pid=opt_data['pid']), thread_ts=thread_ts)
            return True
        _execute_fork(opt_data, channel_id, say, thread_ts, user_id, initial_input,
                      request_msg_ts=request_msg_ts)
    else:
        # ブリッジセッション
        s = opt_data["session"]
        _execute_fork_session(
            source_session_id=s.claude_session_id,
            cwd=s.working_dir,
            channel_id=channel_id,
            say=say,
            thread_ts=thread_ts,
            user_id=user_id,
            initial_input=initial_input,
            source_label=s.display_label or s.claude_session_id[:12],
            request_msg_ts=request_msg_ts,
        )
    return True


# ---------------------------------------------------------------------------
# bind コマンド — ターミナルのclaude CLIにライブ接続
# ---------------------------------------------------------------------------

def _handle_bind(rest: str, say, thread_ts, channel_id: str, user_id: str):
    """bind <PID> — 実行中のclaude CLIプロセスにライブ接続"""
    try:
        target_pid = int(rest.strip())
    except ValueError:
        say(text=t("error_pid_not_number"), thread_ts=thread_ts)
        return

    instances = detect_running_claude_instances()
    if not instances:
        say(text=t("bind_no_instances"), thread_ts=thread_ts)
        return

    tracked_pids = {data["pid"] for data in instance_threads.values()}
    candidates = [i for i in instances if i["pid"] not in tracked_pids]

    matched = [i for i in candidates if i["pid"] == target_pid]
    if not matched:
        if target_pid in tracked_pids:
            say(text=t("bind_pid_already_tracked", pid=target_pid), thread_ts=thread_ts)
        else:
            say(text=t("bind_pid_not_found", pid=target_pid), thread_ts=thread_ts)
        return

    _execute_bind(matched[0], channel_id, say, thread_ts, user_id)


def _handle_bind_list(say, thread_ts, channel_id: str, user_id: str):
    """bind（引数なし）: バインド可能なプロセスリスト表示"""
    instances = detect_running_claude_instances()
    tracked_pids = {data["pid"] for data in instance_threads.values()}
    candidates = [i for i in instances if i["pid"] not in tracked_pids] if instances else []

    if not candidates:
        say(text=t("bind_no_bindable"), thread_ts=thread_ts)
        return

    lines = [t("bind_list_header")]
    for i, inst in enumerate(candidates, 1):
        lines.append(f"  `{i}` — PID {inst['pid']}  :file_folder: `{inst['cwd']}`  :clock1: {inst['etime']}")
    lines.append(t("bind_select_or_cancel"))
    say(text="\n".join(lines), thread_ts=thread_ts)

    pending_bind_selections[channel_id] = {
        "instances": candidates,
        "thread_ts": thread_ts,
        "user_id": user_id,
    }


def _handle_bind_selection(text: str, say, channel_id: str) -> bool:
    """pending_bind_selections の番号選択/キャンセルを処理。
    処理した場合 True、fallthrough の場合 False を返す。"""
    if channel_id not in pending_bind_selections:
        return False

    selection = pending_bind_selections[channel_id]
    instances = selection["instances"]
    thread_ts = selection["thread_ts"]
    user_id = selection["user_id"]
    input_text = text.strip().lower()

    if input_text == "cancel":
        del pending_bind_selections[channel_id]
        say(text=t("bind_cancelled"), thread_ts=thread_ts)
        return True

    try:
        num = int(input_text)
    except ValueError:
        return False

    if num < 1 or num > len(instances):
        say(text=t("error_enter_number_range", max=len(instances)), thread_ts=thread_ts)
        return True

    selected = instances[num - 1]
    del pending_bind_selections[channel_id]

    if not _is_process_alive(selected["pid"]):
        say(text=t("bind_pid_exited", pid=selected['pid']), thread_ts=thread_ts)
        return True

    _execute_bind(selected, channel_id, say, thread_ts, user_id)
    return True


def _execute_bind(inst_info: dict, channel_id: str, say, thread_ts, user_id: str):
    """バインド実行: プロセスのライブI/Oをスレッドに接続"""
    pid = inst_info["pid"]
    cwd = inst_info["cwd"]
    tty = inst_info["tty"]

    # プロセス生存確認
    if not _is_process_alive(pid):
        say(text=t("bind_pid_exited", pid=pid), thread_ts=thread_ts)
        return

    # JONLから session_id を取得
    jsonl_path = _find_session_jsonl(cwd)
    session_id = None
    if jsonl_path:
        dummy_task = Task(id=0, prompt="(bind)")
        dummy_session = type("_S", (), {"claude_session_id": None})()
        _extract_session_info_from_jsonl(dummy_task, dummy_session, jsonl_path)
        session_id = dummy_session.claude_session_id

    # Project/Session 作成
    project = runner.get_or_create_project(channel_id)

    # 新しいトップレベルメッセージ（スレッド開始）
    sid_info = f"\n_Session: `{session_id[:12]}...`_" if session_id else ""
    resp = slack_client.chat_postMessage(
        channel=channel_id,
        text=t("bind_start", pid=pid, cwd=cwd, sid_info=sid_info),
    )
    bind_thread_ts = resp["ts"]

    # Session 作成
    session = project.get_or_create_session(bind_thread_ts)
    session.working_dir = cwd
    if session_id:
        session.claude_session_id = session_id
    runner.save_sessions()
    runner.record_directory(channel_id, cwd)

    # instance_threads に登録
    bind_inst = {
        "pid": pid,
        "cwd": cwd,
        "tty": tty,
        "session": session,
        "display_prefix": f"PID {pid}",
        "skip_exit_message": False,
        "bind_mode": "live",
        "fixed_jsonl": False,
    }
    if jsonl_path:
        bind_inst["jsonl_path"] = jsonl_path
        _monitored_jsonl_paths.add(jsonl_path)

    instance_threads[bind_thread_ts] = bind_inst

    # JSONL監視スレッド起動（JSONL未発見でもfixed_jsonl=Falseで動的に発見）
    monitor_thread = threading.Thread(
        target=_bind_monitor_wrapper,
        args=(bind_inst, bind_thread_ts, channel_id, session, jsonl_path),
        daemon=True,
    )
    monitor_thread.start()


def _bind_monitor_wrapper(inst: dict, thread_ts: str, channel_id: str,
                          session: "Session", jsonl_path: str):
    """バインド用JSONL監視ラッパー: 監視終了後にクリーンアップ"""
    try:
        _monitor_session_jsonl(inst, thread_ts, channel_id, slack_client)
    except Exception as e:
        logger.error("Bind JSONL monitor error: %s", e, exc_info=True)
    finally:
        _monitored_jsonl_paths.discard(jsonl_path)
        instance_threads.pop(thread_ts, None)
        logger.info("Bind monitor ended: pid=%s thread=%s", inst.get("pid"), thread_ts)


_bridge_pid = os.getpid()

def _is_descendant_of_bridge(pid: int) -> bool:
    """pidがbridgeプロセスの子孫かどうかをPPIDチェーンで判定。
    bridgeが起動したサブプロセスやそのサブエージェント等を除外するために使用。"""
    current = pid
    visited = set()
    while current > 1:
        if current == _bridge_pid:
            return True
        if current in visited:
            break  # ループ防止
        visited.add(current)
        try:
            result = subprocess.run(
                ["ps", "-p", str(current), "-o", "ppid="],
                capture_output=True, text=True, timeout=5,
            )
            ppid_str = result.stdout.strip()
            if not ppid_str:
                break
            current = int(ppid_str)
        except Exception:
            break
    return False


def _detect_external_takeover(inst: dict, session: "Session") -> Optional[dict]:
    """bridge起動タスク実行中に、同じsession_idで外部プロセスが起動したか検出。
    検出した場合、外部インスタンス情報を返す。"""
    session_id = session.claude_session_id
    if not session_id:
        return None
    bridge_pid = inst.get("pid")
    bridge_jsonl = inst.get("jsonl_path")

    instances = detect_running_claude_instances()
    for ext_inst in instances:
        if ext_inst["pid"] == bridge_pid:
            continue
        # bridgeプロセスの子孫（サブエージェント等）は外部ではない
        if _is_descendant_of_bridge(ext_inst["pid"]):
            continue
        # 外部プロセスのJSONLからsession_idを確認
        ext_jsonl = _find_session_jsonl(ext_inst["cwd"], exclude_paths=_monitored_jsonl_paths)
        if not ext_jsonl:
            # 未監視のJSONLが見つからない場合:
            # 正規テイクオーバー（--resume で同じセッションを再開）の可能性をチェック。
            # --resume プロセスは既存のJSONLファイル（bridge監視中）に書き込むため、
            # exclude_pathsで除外されてここに来る。
            if not bridge_jsonl:
                continue
            try:
                result = subprocess.run(
                    ["ps", "-p", str(ext_inst["pid"]), "-o", "args="],
                    capture_output=True, text=True, timeout=5,
                )
                args_str = result.stdout.strip()
                if "--resume" not in args_str:
                    continue  # 新規セッション → テイクオーバーではない
                # --resume の引数から session_id を確認
                tokens = args_str.split()
                resume_sid = None
                for idx, tok in enumerate(tokens):
                    if tok == "--resume" and idx + 1 < len(tokens):
                        candidate = tokens[idx + 1]
                        if not candidate.startswith("-"):
                            resume_sid = candidate
                        break
                if resume_sid is not None:
                    # 明示的にセッションIDが指定されている場合: 完全一致 or 前方一致
                    if resume_sid != session_id and not session_id.startswith(resume_sid):
                        continue  # 別セッションの --resume → テイクオーバーではない
                else:
                    # --resume 引数なし（対話的ピッカー使用）の場合:
                    # lsof で外部プロセスが bridge の JSONL またはセッション関連ファイルを
                    # 開いているか確認。claude CLIはJSONLを常時オープンしないため、
                    # セッションIDを含む任意のファイルパスもチェックする。
                    try:
                        lsof_result = subprocess.run(
                            ["lsof", "-p", str(ext_inst["pid"]), "-F", "n"],
                            capture_output=True, text=True, timeout=5,
                        )
                        lsof_lines = [line[1:] for line in lsof_result.stdout.splitlines()
                                       if line.startswith("n")]
                        if not any(bridge_jsonl == f or session_id in f for f in lsof_lines):
                            continue  # セッション関連ファイルを開いていない → 別セッション
                    except Exception:
                        continue
            except Exception:
                continue
            ext_jsonl = bridge_jsonl
        try:
            with open(ext_jsonl, "r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= 10:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = entry.get("message", {})
                    sid = (msg.get("sessionId") if isinstance(msg, dict) else None) or entry.get("sessionId")
                    if sid == session_id:
                        return {
                            "pid": ext_inst["pid"],
                            "cwd": ext_inst["cwd"],
                            "tty": ext_inst["tty"],
                            "etime": ext_inst["etime"],
                            "jsonl_path": ext_jsonl,
                        }
        except OSError:
            continue
    return None


def _handle_external_takeover(inst: dict, external: dict, session: "Session",
                              thread_ts: str, channel_id: str, client: WebClient):
    """外部プロセスがセッションを引き継いだ場合の処理:
    bridge起動のサブプロセスを終了し、外部プロセスの監視に切り替える。"""
    import signal
    old_pid = inst.get("pid")
    new_pid = external["pid"]

    logger.info("External takeover detected: old_pid=%s new_pid=%s session=%s thread=%s",
                old_pid, new_pid, session.claude_session_id[:16] if session.claude_session_id else "?", thread_ts)

    # bridge起動のサブプロセスを終了
    task_ref = inst.get("task")
    if task_ref and task_ref.process and task_ref.process.poll() is None:
        try:
            task_ref.process.send_signal(signal.SIGTERM)
            try:
                task_ref.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                task_ref.process.kill()
                task_ref.process.wait(timeout=3)
        except Exception as e:
            logger.warning("Failed to kill bridge subprocess pid=%s: %s", old_pid, e)

    # master_fd をクローズ
    old_master_fd = inst.pop("master_fd", None)
    if old_master_fd is not None:
        try:
            os.close(old_master_fd)
        except OSError:
            pass
        if task_ref:
            task_ref.master_fd = None

    # inst を外部プロセスに更新
    inst["pid"] = new_pid
    inst["cwd"] = external["cwd"]
    inst["tty"] = external["tty"]
    inst["bind_mode"] = "live"
    inst["external_takeover"] = True
    inst["skip_exit_message"] = False
    if external.get("jsonl_path"):
        inst["jsonl_path"] = external["jsonl_path"]
        inst["fixed_jsonl"] = False

    # タスクステータスを更新
    if task_ref:
        task_ref.status = TaskStatus.COMPLETED
        task_ref.completed_at = datetime.now()

    # Slack通知（外部テイクオーバー専用メッセージ）
    try:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=t("external_takeover", pid=new_pid),
        )
    except Exception:
        pass


# ── アイドルセッション外部テイクオーバー検出 ──────────────────

_IDLE_TAKEOVER_INTERVAL = 15  # 秒（アクティブタスクの検出間隔と同程度）
_idle_session_jsonl_sizes: dict[str, tuple[str, int]] = {}  # session_id -> (jsonl_path, file_size)


def _check_idle_session_takeovers():
    """全アイドルセッションに対して外部テイクオーバーを検出し、bind監視を開始する。"""
    # 1. runner.projects から全セッションを収集
    #    - claude_session_id がある
    #    - instance_threads に未登録（アイドル状態）
    active_thread_tss = set(instance_threads.keys())
    known_sessions: dict[str, tuple["Session", "Project"]] = {}
    for channel_id, project in list(runner.projects.items()):
        for thread_ts, session in list(project.sessions.items()):
            if not session.claude_session_id:
                continue
            if thread_ts in active_thread_tss:
                continue  # アクティブなタスクまたはbind中
            sid = session.claude_session_id
            # 同一session_idが複数ある場合、最新のセッションを優先
            if sid in known_sessions:
                existing_session = known_sessions[sid][0]
                if session.created_at > existing_session.created_at:
                    known_sessions[sid] = (session, project)
            else:
                known_sessions[sid] = (session, project)

    if not known_sessions:
        # known_sessions が空でもクリーンアップは必要
        for sid in list(_idle_session_jsonl_sizes):
            _idle_session_jsonl_sizes.pop(sid, None)
        return

    # 2. JONLサイズ成長チェック（対話ピッカー検出用）
    grown_sessions: dict[str, tuple["Session", "Project", str]] = {}  # sid -> (session, project, jsonl_path)
    for sid, (session, project) in known_sessions.items():
        prev = _idle_session_jsonl_sizes.get(sid)
        if prev is not None:
            jsonl_path, prev_size = prev
            try:
                current_size = os.path.getsize(jsonl_path)
                if current_size > prev_size:
                    grown_sessions[sid] = (session, project, jsonl_path)
                _idle_session_jsonl_sizes[sid] = (jsonl_path, current_size)
            except OSError:
                _idle_session_jsonl_sizes.pop(sid, None)
        else:
            jsonl_path = _find_jsonl_path_for_session_id(sid)
            if jsonl_path:
                try:
                    _idle_session_jsonl_sizes[sid] = (jsonl_path, os.path.getsize(jsonl_path))
                except OSError:
                    pass
    # 不要エントリをクリーンアップ
    for sid in list(_idle_session_jsonl_sizes):
        if sid not in known_sessions:
            del _idle_session_jsonl_sizes[sid]

    # 3. 外部claudeプロセスを取得（成長チェック結果がなくてもlsofフォールバックのために続行）
    instances = detect_running_claude_instances()
    if not instances:
        return

    # 4. フィルタ: bridge子孫 / instance_threadsのアクティブPID を除外
    active_pids = {inst.get("pid") for inst in instance_threads.values() if inst.get("pid")}
    candidates = []
    for ext in instances:
        if ext["pid"] in active_pids:
            continue
        if _is_descendant_of_bridge(ext["pid"]):
            continue
        candidates.append(ext)

    if not candidates:
        return

    # 5. 各候補の --resume 引数をチェック
    for ext in candidates:
        try:
            result = subprocess.run(
                ["ps", "-p", str(ext["pid"]), "-o", "args="],
                capture_output=True, text=True, timeout=5,
            )
            args_str = result.stdout.strip()
            if "--resume" not in args_str:
                continue  # 新規セッションはテイクオーバーではない

            # --resume の引数からsession_idを確認
            tokens = args_str.split()
            resume_sid = None
            for idx, tok in enumerate(tokens):
                if tok == "--resume" and idx + 1 < len(tokens):
                    candidate_tok = tokens[idx + 1]
                    if not candidate_tok.startswith("-"):
                        resume_sid = candidate_tok
                    break

            matched_session = None
            matched_project = None

            if resume_sid is not None:
                # 明示的にセッションIDが指定: 完全一致 or 前方一致
                for sid, (session, project) in known_sessions.items():
                    if resume_sid == sid or sid.startswith(resume_sid):
                        matched_session = session
                        matched_project = project
                        break
            else:
                # --resume 引数なし（対話的ピッカー使用）:
                # 方法1: JONLファイルサイズ成長チェック（ステップ2で検出済み）
                for sid, (sess, proj, jp) in list(grown_sessions.items()):
                    matched_session = sess
                    matched_project = proj
                    ext["_jsonl_path"] = jp
                    grown_sessions.pop(sid)  # 消費済み
                    break
                # 方法2: lsof フォールバック（JONLが偶然開いていれば検出可能）
                if not matched_session:
                    try:
                        lsof_result = subprocess.run(
                            ["lsof", "-p", str(ext["pid"]), "-F", "n"],
                            capture_output=True, text=True, timeout=5,
                        )
                        jsonl_sid = None
                        for line in lsof_result.stdout.splitlines():
                            if line.startswith("n") and line.endswith(".jsonl"):
                                jsonl_file = line[1:]
                                try:
                                    with open(jsonl_file, "r", encoding="utf-8") as f:
                                        for i, jline in enumerate(f):
                                            if i >= 10:
                                                break
                                            jline = jline.strip()
                                            if not jline:
                                                continue
                                            try:
                                                entry = json.loads(jline)
                                            except json.JSONDecodeError:
                                                continue
                                            msg = entry.get("message", {})
                                            sid = (msg.get("sessionId") if isinstance(msg, dict) else None) or entry.get("sessionId")
                                            if sid and sid in known_sessions:
                                                jsonl_sid = sid
                                                ext["_jsonl_path"] = jsonl_file
                                                break
                                        if jsonl_sid:
                                            break
                                except OSError:
                                    continue
                        if jsonl_sid:
                            matched_session, matched_project = known_sessions[jsonl_sid]
                    except Exception:
                        pass

            if matched_session and matched_project:
                _initiate_idle_takeover(ext, matched_session, matched_project)
                # マッチしたsession_idを除去（同一ループで二重処理を防止）
                known_sessions.pop(matched_session.claude_session_id, None)
        except Exception as e:
            logger.debug("Idle takeover check error for PID %s: %s", ext.get("pid"), e)
            continue


def _initiate_idle_takeover(ext: dict, session: "Session", project: "Project"):
    """アイドルセッションの外部テイクオーバーを開始: 既存スレッドにbind監視を接続する。"""
    thread_ts = session.thread_ts
    channel_id = session.channel_id
    pid = ext["pid"]

    # 競合防止: instance_threads に既に登録されていないか再チェック
    if thread_ts in instance_threads:
        return

    logger.info("Idle session takeover detected: pid=%s session=%s thread=%s",
                pid, session.claude_session_id[:16] if session.claude_session_id else "?", thread_ts)

    # JONLパスを決定
    jsonl_path = ext.get("_jsonl_path")  # _check_idle_session_takeovers で取得済みの場合
    if not jsonl_path:
        # lsof で外部プロセスが開いている .jsonl を探す
        try:
            lsof_result = subprocess.run(
                ["lsof", "-p", str(pid), "-F", "n"],
                capture_output=True, text=True, timeout=5,
            )
            for line in lsof_result.stdout.splitlines():
                if line.startswith("n") and line.endswith(".jsonl"):
                    jsonl_path = line[1:]
                    break
        except Exception:
            pass
    if not jsonl_path:
        # フォールバック: セッションのworking_dirからJSONLを探す
        jsonl_path = _find_session_jsonl(session.working_dir)
    if not jsonl_path:
        logger.warning("Idle takeover: no JSONL found for pid=%s session=%s, skipping",
                       pid, session.claude_session_id[:16] if session.claude_session_id else "?")
        return

    # inst dict を構築
    inst = {
        "pid": pid,
        "cwd": ext["cwd"],
        "tty": ext["tty"],
        "session": session,
        "display_prefix": f"PID {pid}",
        "skip_exit_message": False,
        "bind_mode": "live",
        "jsonl_path": jsonl_path,
        "fixed_jsonl": False,
    }

    # instance_threads に登録（再チェック付き）
    if thread_ts in instance_threads:
        return  # 別スレッドが先に登録した
    instance_threads[thread_ts] = inst
    _monitored_jsonl_paths.add(jsonl_path)

    # Slack スレッドに通知
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=t("bind_session_takeover", pid=pid),
        )
    except Exception as e:
        logger.warning("Idle takeover notification error: %s", e)

    # デーモンスレッドで監視開始
    monitor_thread = threading.Thread(
        target=_bind_monitor_wrapper,
        args=(inst, thread_ts, channel_id, session, jsonl_path),
        daemon=True,
    )
    monitor_thread.start()


def _idle_takeover_monitor_loop():
    """バックグラウンドスレッド: 全アイドルセッションの外部テイクオーバーを定期検出する。"""
    while not _shutdown_event.is_set():
        _shutdown_event.wait(_IDLE_TAKEOVER_INTERVAL)
        if _shutdown_event.is_set():
            break
        try:
            _check_idle_session_takeovers()
        except Exception as e:
            logger.error("Idle takeover monitor error: %s", e, exc_info=True)


def _handle_bare_task(text: str, event: dict, say, channel_id: str, user_id: str):
    """ベアタスク: ルート設定時は即実行、なければフォーク候補 + ディレクトリ履歴を表示し選択を待つ"""
    thread_ts = event.get("ts")
    request_msg_ts = event.get("ts")
    files = _resolve_event_files(event, channel_id)

    # ルートディレクトリが設定されている場合は即実行
    root = runner.get_channel_root(channel_id)
    if root:
        if not os.path.isdir(root):
            say(
                text=t("root_dir_not_found", path=root),
                thread_ts=thread_ts,
            )
            return
        _start_task_in_dir(root, text, say, thread_ts, channel_id, user_id, files,
                           request_msg_ts=request_msg_ts)
        return

    # フォーク候補を収集
    instances = detect_running_claude_instances()
    tracked_pids = {data["pid"] for data in instance_threads.values()}
    fork_candidates = [i for i in instances if i["pid"] not in tracked_pids] if instances else []

    # ディレクトリ履歴を収集
    dir_history = runner.directory_history.get(channel_id, [])

    # 選択肢がない場合
    if not fork_candidates and not dir_history:
        say(
            text=t("error_need_working_dir"),
            thread_ts=thread_ts,
        )
        return

    # 選択肢リストを構築
    options = []  # (type, data) のリスト
    lines = [t("dir_select_header")]

    idx = 1
    if fork_candidates:
        lines.append(t("dir_forkable_header"))
        for inst in fork_candidates:
            lines.append(f"  `{idx}` — :fork_and_knife: PID {inst['pid']}  :file_folder: `{inst['cwd']}`  :clock1: {inst['etime']}")
            options.append(("fork", inst))
            idx += 1

    if dir_history:
        lines.append(t("dir_recent_header"))
        for d in dir_history:
            lines.append(f"  `{idx}` — :file_folder: `{d}`")
            options.append(("dir", d))
            idx += 1

    lines.append(t("dir_select_prompt"))
    say(text="\n".join(lines), thread_ts=thread_ts)

    pending_directory_requests[thread_ts] = {
        "prompt": text,
        "user_id": user_id,
        "channel_id": channel_id,
        "options": options,
        "files": files,
        "request_msg_ts": request_msg_ts,
    }


def _handle_directory_selection(text: str, say, thread_ts: str) -> bool:
    """ベアタスクのディレクトリ選択処理。
    処理した場合 True、fallthrough の場合 False を返す。"""
    if thread_ts not in pending_directory_requests:
        return False

    req = pending_directory_requests[thread_ts]
    prompt = req["prompt"]
    user_id = req["user_id"]
    channel_id = req["channel_id"]
    options = req["options"]
    files = req.get("files")
    request_msg_ts = req.get("request_msg_ts")
    input_text = text.strip()

    # cancel
    if input_text.lower() == "cancel":
        del pending_directory_requests[thread_ts]
        say(text=t("dir_cancelled"), thread_ts=thread_ts)
        return True

    # 番号入力
    num_match = re.match(r"^\s*(\d+)\s*$", input_text)
    if num_match:
        num = int(num_match.group(1))
        if num < 1 or num > len(options):
            say(text=t("error_enter_number_range", max=len(options)), thread_ts=thread_ts)
            return True

        opt_type, opt_data = options[num - 1]
        del pending_directory_requests[thread_ts]

        if opt_type == "fork":
            # フォーク候補選択
            if not _is_process_alive(opt_data["pid"]):
                say(text=t("fork_pid_exited", pid=opt_data['pid']), thread_ts=thread_ts)
                return True
            _execute_fork(opt_data, channel_id, say, thread_ts, user_id, prompt,
                          request_msg_ts=request_msg_ts)
            return True
        else:
            # ディレクトリ選択
            if not os.path.isdir(opt_data):
                say(text=t("error_dir_not_found", path=opt_data), thread_ts=thread_ts)
                return True
            _start_task_in_dir(opt_data, prompt, say, thread_ts, channel_id, user_id, files,
                               request_msg_ts=request_msg_ts)
            return True

    # 絶対パス入力
    expanded = os.path.expanduser(input_text)
    if os.path.isabs(expanded):
        del pending_directory_requests[thread_ts]
        if not os.path.isdir(expanded):
            say(text=t("error_dir_not_found", path=expanded), thread_ts=thread_ts)
            return True
        _start_task_in_dir(expanded, prompt, say, thread_ts, channel_id, user_id, files,
                           request_msg_ts=request_msg_ts)
        return True

    # 数字でもcancelでも絶対パスでもない → fallthroughしない（選択中なので）
    say(text=t("error_enter_number_path_cancel"), thread_ts=thread_ts)
    return True


def _slack_reachable(timeout: float = 3.0) -> bool:
    """Slack に TCP 到達可能かの簡易チェック。
    Socket Mode の連続エラー時に「回線/Slack側ダウン（再起動しても無力）」と
    「プロセス内部状態の破損（再起動で回復しうる）」を切り分けるために使う。
    HTTPS(443) への接続可否で判定（WebSocket も同じ回線・名前解決を通るため代理になる）。"""
    for host in ("slack.com", "api.slack.com"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


# ── エントリーポイント ────────────────────────────────────
# ════════════════════════════════════════════════════════════
#  GitHub 更新チェック / 自動アップデート
# ════════════════════════════════════════════════════════════
# 定期的に GitHub 上の最新コミットと手元の版を比較し、差があれば
# NOTIFICATION_CHANNEL に通知する。通知のボタンから適用すると、
# git リポジトリなら `git pull --ff-only`、そうでなければ tarball を
# ダウンロードして展開し、最後にブリッジを再起動する。

# 同じ更新を毎回通知しないためのメモリ内デデュープ（再起動でリセット）
_github_last_notified_sha: Optional[str] = None

# 自動アップデート時に上書き・削除しないユーザー資産（.env や各種状態ファイル）
_UPDATE_PROTECTED = {
    ".env", ".git", "venv", ".venv", "__pycache__", "node_modules",
    "directory_history.json", "channel_roots.json", "project_tools.json",
    "sessions.json", "session_history.json", "interrupted_tasks.json",
    "github_update_state.json",
}


def _post_notification(text: str):
    """NOTIFICATION_CHANNEL へ投稿する（未設定ならログのみ）。"""
    if not NOTIFICATION_CHANNEL:
        logger.info("[notify] %s", text)
        return
    try:
        slack_client.chat_postMessage(channel=NOTIFICATION_CHANNEL, text=text)
    except Exception as e:
        logger.warning("通知送信に失敗: %s", e)


def _is_git_repo() -> bool:
    return os.path.isdir(os.path.join(REPO_DIR, ".git"))


def _git(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", REPO_DIR, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _github_api_get(path: str) -> Optional[dict]:
    """GitHub REST API を叩いて JSON を返す。失敗時 None。"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "claude-slack-bridge",
    }
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("GitHub API 取得失敗 %s: %s", path, e)
        return None


def _remote_latest_sha() -> Optional[str]:
    """監視対象ブランチの GitHub 上の最新コミット SHA を取得する。"""
    if _is_git_repo():
        r = _git("fetch", "origin", GITHUB_BRANCH, timeout=60)
        if r.returncode != 0:
            logger.warning("git fetch 失敗: %s", r.stderr.strip())
            return None
        r = _git("rev-parse", f"origin/{GITHUB_BRANCH}")
        return r.stdout.strip() or None if r.returncode == 0 else None
    data = _github_api_get(f"commits/{GITHUB_BRANCH}")
    return data.get("sha") if data else None


def _local_current_sha() -> Optional[str]:
    """手元に入っている版の SHA。git なら HEAD、非 git なら状態ファイル。"""
    if _is_git_repo():
        r = _git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        with open(GITHUB_UPDATE_STATE_FILE) as f:
            return json.load(f).get("installed_sha")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_installed_sha(sha: str):
    """非 git インストール時の導入済み SHA を記録する。"""
    try:
        with open(GITHUB_UPDATE_STATE_FILE, "w") as f:
            json.dump({"installed_sha": sha}, f)
    except Exception as e:
        logger.warning("installed_sha 保存失敗: %s", e)


def _check_github_updates():
    """GitHub 上の最新版と手元版を比較し、更新があれば通知する。"""
    global _github_last_notified_sha
    remote = _remote_latest_sha()
    if not remote:
        return

    if _is_git_repo():
        # HEAD..origin/branch のコミット数が 0 より大きければ「遅れている」
        r = _git("rev-list", "--count", f"HEAD..origin/{GITHUB_BRANCH}")
        behind = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
        if behind <= 0:
            return
    else:
        # 非 git: ベースライン未記録なら現在の最新を「導入済み」とみなし記録（初回は通知しない）
        local = _local_current_sha()
        if local is None:
            _save_installed_sha(remote)
            logger.info("GitHub更新チェック: 非gitインストールのベースラインを記録 %s", remote[:8])
            return
        if remote == local:
            return

    if remote == _github_last_notified_sha:
        return  # 同じ更新は二重通知しない
    _github_last_notified_sha = remote
    logger.info("GitHub更新を検出: %s", remote[:8])
    _notify_update_available(remote)


def _notify_update_available(remote_sha: str):
    """「アップデートがあります」を適用ボタン付きで通知する。"""
    if not NOTIFICATION_CHANNEL:
        logger.info("[notify] %s", t("update_available"))
        return
    try:
        slack_client.chat_postMessage(
            channel=NOTIFICATION_CHANNEL,
            text=t("update_available"),
            blocks=[
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": t("update_available")}},
                {"type": "actions", "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": t("update_apply_button")},
                    "style": "primary",
                    "action_id": "apply_update",
                    "value": remote_sha,
                }]},
            ],
        )
    except Exception as e:
        logger.warning("更新通知の送信に失敗: %s", e)


@app.action("apply_update")
def handle_apply_update(ack, body):
    ack()
    user_id = body.get("user", {}).get("id")
    if not user_id or not _is_user_allowed(user_id):
        return
    _remove_action_buttons(body, t("update_applying"))
    threading.Thread(target=_apply_update, daemon=True).start()


def _apply_update():
    """更新を適用する。git なら pull、非 git なら tarball を DL→展開。成功時は再起動。"""
    try:
        if _is_git_repo():
            ok, msg = _apply_update_git()
        else:
            ok, msg = _apply_update_download()
    except Exception as e:
        logger.error("更新適用エラー: %s", e, exc_info=True)
        ok, msg = False, str(e)

    if not ok:
        _post_notification(t("update_failed", reason=msg or "unknown"))
        return

    _post_notification(t("update_done"))
    # 再起動（kickstart は launchd 側で完遂するため、呼び出し元が死んでも安全）
    try:
        subprocess.Popen(
            [os.path.join(REPO_DIR, "scripts", "restart.sh")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.error("再起動に失敗: %s", e)


def _apply_update_git() -> tuple[bool, str]:
    """git pull --ff-only で更新する。ローカル変更等で ff できなければ失敗を返す。"""
    r = _git("pull", "--ff-only", "origin", GITHUB_BRANCH, timeout=120)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr.strip() or r.stdout.strip() or "git pull に失敗しました")


def _apply_update_download() -> tuple[bool, str]:
    """GitHub の tarball をダウンロードし、ユーザー資産を保護しつつ展開する。"""
    import tempfile
    import tarfile
    import shutil

    remote_sha = _remote_latest_sha()
    url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.tar.gz"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = os.path.join(tmp, "src.tar.gz")
            req = urllib.request.Request(url, headers={"User-Agent": "claude-slack-bridge"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(tar_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            with tarfile.open(tar_path) as tf:
                tf.extractall(tmp)
            # 展開トップディレクトリ（<repo>-<branch>/）を特定
            roots = [d for d in os.listdir(tmp)
                     if os.path.isdir(os.path.join(tmp, d))]
            if not roots:
                return False, "アーカイブの展開に失敗しました"
            _copy_tree_preserving(os.path.join(tmp, roots[0]), REPO_DIR)
        if remote_sha:
            _save_installed_sha(remote_sha)
        return True, ""
    except Exception as e:
        return False, str(e)


def _copy_tree_preserving(src_root: str, dst_root: str):
    """src_root の中身を dst_root へコピーする。_UPDATE_PROTECTED は触らない。
    上流から消えたファイルの削除は行わない（ユーザー資産を守るため安全側）。"""
    import shutil
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        # 保護対象ディレクトリには下りない
        dirnames[:] = [d for d in dirnames if d not in _UPDATE_PROTECTED]
        rel_top = "" if rel == "." else rel.split(os.sep)[0]
        if rel_top in _UPDATE_PROTECTED:
            continue
        for fn in filenames:
            if fn in _UPDATE_PROTECTED:
                continue
            src = os.path.join(dirpath, fn)
            dst = os.path.join(dst_root, fn) if rel == "." else os.path.join(dst_root, rel, fn)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)


def _github_update_monitor_loop():
    """バックグラウンドスレッド: 定期的に GitHub 更新をチェックする。"""
    _shutdown_event.wait(30)  # 起動直後の初期化と被らないよう少し待つ
    while not _shutdown_event.is_set():
        try:
            _check_github_updates()
        except Exception as e:
            logger.error("GitHub更新チェックエラー: %s", e, exc_info=True)
        _shutdown_event.wait(GITHUB_UPDATE_INTERVAL)


def main():
    logger.info("=" * 55)
    logger.info("  Claude Code ⇔ Slack Bridge")
    logger.info("=" * 55)
    logger.info("  Admin:       %s", ADMIN_SLACK_USER_ID)
    logger.info("  Tools:       %s", DEFAULT_ALLOWED_TOOLS)
    logger.info("  Allowed Users:    %s", SLACK_ALLOWED_USERS or "(none)")
    logger.info("  Allowed Channels: %s", SLACK_ALLOWED_CHANNELS or "(none)")
    logger.info("  Notification:     %s", NOTIFICATION_CHANNEL or "(log only)")
    logger.info("  GitHub Update:    %s",
                f"有効 ({GITHUB_UPDATE_INTERVAL}s, {GITHUB_REPO}@{GITHUB_BRANCH})"
                if GITHUB_UPDATE_CHECK else "無効")
    logger.info("=" * 55)
    logger.info("Press Ctrl+C to stop")

    # ディレクトリ履歴・チャンネルルートを読み込み
    runner.load_directory_history()
    channel_roots = runner.load_channel_roots()
    logger.info("  Channel Roots:    %d", len(channel_roots))
    saved_sessions = runner.load_sessions()
    total_sessions = sum(len(v) for v in saved_sessions.values())
    logger.info("  Saved Sessions:   %d", total_sessions)

    # アイドルテイクオーバー検出のため全プロジェクトを事前ロード
    for channel_id in saved_sessions:
        runner.get_or_create_project(channel_id)

    # 起動通知
    if NOTIFICATION_CHANNEL:
        try:
            slack_client.chat_postMessage(
                channel=NOTIFICATION_CHANNEL,
                text=t("notify_startup"),
            )
        except Exception as e:
            logger.warning("Failed to send Slack startup notification: %s", e)

    # Bot Token スコープ検証: 不足があれば再認可を促す通知を送る
    _check_bot_scopes()

    # 中断タスク復帰通知: 前回プロセスが進行中タスクを残したまま死んだ場合、
    # 該当スレッドに「中断されました」を投稿する。
    interrupted = runner.load_and_clear_interrupted_tasks()
    if interrupted:
        logger.info("Posting interruption notice for %d task(s)", len(interrupted))
        for entry in interrupted:
            emoji = entry.get("label_emoji") or ""
            name = entry.get("label_name") or ""
            label = (f"{emoji} {name}".strip()) or emoji or "(session)"
            prompt = (entry.get("prompt") or "").strip()
            prompt_short = prompt[:200] + ("…" if len(prompt) > 200 else "")
            text = t("task_interrupted_by_restart",
                     label=label,
                     prompt=prompt_short or "(empty)")
            try:
                slack_client.chat_postMessage(
                    channel=entry["channel_id"],
                    thread_ts=entry["thread_ts"],
                    text=text,
                )
                logger.info("Interruption notice posted thread=%s", entry.get("thread_ts"))
            except Exception as e:
                logger.warning(
                    "Failed to post interruption notice thread=%s err=%s",
                    entry.get("thread_ts"), e,
                )

    handler = SocketModeHandler(app, SLACK_APP_TOKEN, trace_enabled=True)
    # ── Socket Mode の接続健全性監視 → 必要なら再起動 ──
    # 【再接続は SDK 内蔵監視(current_app_monitor)に一本化する】
    # SDK は check_state(=ping) と recv の両パスの失敗を検知し connect_to_new_endpoint で
    # 自動再接続する。ここでブリッジ側からも再接続すると「2つの再接続主体が互いの張りたて
    # ソケットを close し合う」レースで wedge するため、ブリッジは再接続を一切行わない。
    # （default_auto_reconnect_enabled は SDK 既定の True のままにし、無効化しない）
    #
    # 【回復判断はエラーコールバックでなく is_connected() のポーリングで行う】
    # on_error_listeners は recv パスでしか発火せず、ping パス(check_state)の失敗を取りこぼす。
    # 実際 2026-06-18 の事故では ping パスで切断したのに自前ループが起動せず、
    # auto_reconnect も無効化していたため誰も再接続せず無限沈黙した。
    # そこで失敗パスに依存しないよう、接続状態そのものを定期ポーリングするのを唯一の真実とする。
    # 一定時間 切断が継続し かつ Slack 到達可 かつ (走行タスク無し or 全ハング) なら、
    # SDK 自動再接続でも回復しない内部状態破損とみなしプロセスを終了し launchd に再起動させる。
    # これで recv / ping / silent いずれの wedge も上限時間内に必ず回復する。
    _SM_HEALTH_CHECK_INTERVAL = 15.0    # 接続状態ポーリング間隔（秒）
    _SM_DISCONNECT_NOTIFY_AFTER = 10.0  # 切断がこの秒数継続したらmacOS通知
    _SM_RESTART_AFTER = 120.0           # 切断がこの秒数継続したら再起動を検討
    # 走行中タスクがこの秒数以上 JSONL 無活動なら「ハング」とみなし、socket wedge 時の
    # 再起動見送りガードを解除する（ハングタスクに人質を取られて socket を復旧できない事故対策）。
    # 正当な長時間 Bash（ビルド等）の巻き添えを避けるため十分大きく取る。--resume で再開可能。
    _TASK_STALE_RESTART_SEC = 900.0
    _sm_disconnect_since: float = 0.0   # 連続切断の開始時刻 (0 = 接続中)
    _sm_disconnect_notified = False     # 切断通知済みフラグ（通知済みの場合のみ復帰通知を出す）

    def _macos_notify(title: str, message: str):
        """macOS 通知センターに通知を表示する"""
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{message}" with title "{title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.debug("macOS notification failed: %s", e)

    def _format_duration(seconds: int) -> str:
        minutes = seconds // 60
        secs = seconds % 60
        if minutes > 0:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    def _socket_health_watchdog_loop():
        """接続状態を実ポーリングし、長時間切断なら再起動する独立 watchdog。
        再接続自体は SDK 監視に委ね、ここは「回復できているか」だけを is_connected() で
        監視する。失敗パス(recv/ping)に依存しないので silent wedge も確実に検知できる。"""
        nonlocal _sm_disconnect_since, _sm_disconnect_notified
        while not _shutdown_event.is_set():
            _shutdown_event.wait(timeout=_SM_HEALTH_CHECK_INTERVAL)
            if _shutdown_event.is_set():
                break
            try:
                connected = handler.client is not None and handler.client.is_connected()
            except Exception:
                connected = False

            now = time.time()
            if connected:
                # 回復: 切断を検知していたら（必要なら）復帰通知してリセット
                if _sm_disconnect_since > 0:
                    elapsed = int(now - _sm_disconnect_since)
                    logger.info("Socket Mode 接続回復（切断 %d秒）", elapsed)
                    if _sm_disconnect_notified:
                        _macos_notify(
                            "Claude Slack Bridge",
                            f"Slack接続が回復しました（切断 {_format_duration(elapsed)}）",
                        )
                _sm_disconnect_since = 0.0
                _sm_disconnect_notified = False
                continue

            # 切断中
            if _sm_disconnect_since == 0.0:
                _sm_disconnect_since = now
                logger.warning("Socket Mode 切断を検知（SDK 自動再接続を待機）")
                continue
            elapsed = int(now - _sm_disconnect_since)

            # 切断が一定継続 → macOS 通知（一度だけ）
            if not _sm_disconnect_notified and elapsed >= _SM_DISCONNECT_NOTIFY_AFTER:
                _sm_disconnect_notified = True
                _macos_notify(
                    "Claude Slack Bridge",
                    f"Slack接続が切断しました（{_format_duration(elapsed)}以上）",
                )

            # 切断が _SM_RESTART_AFTER 継続 → 再起動を検討
            # 再起動は「SDK 自動再接続でも回復しないプロセス内部状態の破損」の最終手段。
            # 次の2ケースは再起動が逆効果なので見送り、継続監視する:
            #   1) 回線/Slack 到達不可 → 再起動しても無力。回線復旧を静かに待つ。
            #   2) 走行中タスクあり → 巻き添えで殺す。タスクは socket に依存せず走り続けられる。
            #      ただし全タスクが長時間ハングしているなら人質を解いて再起動する。
            if elapsed >= _SM_RESTART_AFTER:
                reachable = _slack_reachable()
                has_tasks = runner.has_running_tasks()
                stale = has_tasks and runner.all_running_tasks_stale(_TASK_STALE_RESTART_SEC)
                if reachable and (not has_tasks or stale):
                    logger.critical(
                        "Socket Mode が %d秒間切断（SDK 自動再接続でも回復せず）。"
                        "回線は到達可・%sのため内部状態破損とみなしプロセスを終了し再起動します。",
                        elapsed,
                        ("走行中タスクは全てハング(無活動>%d秒)" % int(_TASK_STALE_RESTART_SEC))
                        if stale else "走行中タスク無し",
                    )
                    _macos_notify(
                        "Claude Slack Bridge",
                        f"Slack接続が{_format_duration(elapsed)}復旧しないため再起動します。",
                    )
                    try:
                        runner.save_sessions()
                    except Exception:
                        pass
                    os._exit(1)
                else:
                    reason = "回線/Slack 到達不可" if not reachable else "走行中タスクあり"
                    logger.warning(
                        "Socket Mode が %d秒間切断だが、%sのため再起動を見送り継続監視します。",
                        elapsed, reason,
                    )

    def _on_socket_error(error: Exception):
        """エラーは可視化のためログするのみ。再接続は SDK 監視に委ね、回復の判断は
        watchdog の is_connected() ポーリングが行う（ping パスのエラーは
        on_error_listeners を発火しないため、状態判断をコールバックに依存させない）。"""
        logger.warning("Socket Mode エラー: %s", error)

    handler.client.on_error_listeners.append(_on_socket_error)

    threading.Thread(
        target=_socket_health_watchdog_loop,
        name="sm-health-watchdog",
        daemon=True,
    ).start()

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        _shutdown_event.set()

        # シャットダウン全体をワーカースレッドで実行し、デッドライン超過時は強制終了
        def _shutdown_worker():
            # 0. セッション保存
            try:
                runner.save_sessions()
            except Exception as e:
                logger.warning("save_sessions on shutdown failed: %s", e)
            # 1. 停止通知（最優先）
            if NOTIFICATION_CHANNEL:
                try:
                    slack_client.chat_postMessage(
                        channel=NOTIFICATION_CHANNEL,
                        text=t("notify_shutdown"),
                    )
                    logger.info("Shutdown notification sent")
                except Exception as e:
                    logger.warning("Failed to send Slack shutdown notification: %s", e)
            # 2. タスクキャンセル
            try:
                runner.cancel_all()
            except Exception as e:
                logger.warning("cancel_all failed: %s", e)
            # 3. Socket Mode切断
            try:
                handler.close()
            except Exception as e:
                logger.warning("handler.close failed: %s", e)

        worker = threading.Thread(target=_shutdown_worker)
        worker.start()
        worker.join(timeout=15)
        if worker.is_alive():
            logger.warning("Shutdown timed out after 15s, forcing exit")
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # アイドルセッション外部テイクオーバー監視スレッド起動
    idle_monitor = threading.Thread(target=_idle_takeover_monitor_loop, daemon=True)
    idle_monitor.start()

    # App Home ダッシュボードの定期更新スレッド起動
    home_poll = threading.Thread(target=_app_home_poll_loop, daemon=True)
    home_poll.start()

    # GitHub 更新チェックスレッド起動
    if GITHUB_UPDATE_CHECK:
        gh_monitor = threading.Thread(target=_github_update_monitor_loop, daemon=True)
        gh_monitor.start()

    handler.start()


if __name__ == "__main__":
    main()
