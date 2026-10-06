# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A bridge that lets users control Claude Code (CLI) from Slack on their Mac. Multiple tasks can run concurrently. Uses Slack Socket Mode (no public URL required).

Channel mode only: Whitelisted users/channels (`SLACK_ALLOWED_USERS`, `SLACK_ALLOWED_CHANNELS`). Requires `@bot` mention for all commands (top-level and in-thread). Thread replies without mention are forwarded to CLI or create resume tasks.

Tasks are started with a directory specified at invocation time via four methods:
- `@bot in <path> <task>` — run in specified directory
- `@bot fork <PID> [<task>]` — fork a running Claude CLI process
- `@bot team [in <path>] <task>` — run with Team Agent (parallel subtasks via multiple agents)
- `@bot <task>` — select from fork candidates / directory history in a thread

Terminal-Slack bidirectional sync:
- `@bot bind <PID>` — live-connect to a running terminal Claude CLI process (JSONL monitoring + input forwarding via AppleScript)
- `@bot bind` — list bindable processes
- Auto-takeover: when a terminal user `claude --resume`s a bridge-spawned session, the bridge detects it and switches to bind mode automatically. The user can find the session via `claude --resume` (no args) which opens an interactive picker listing recent sessions.

## Running

The bridge runs as a macOS LaunchAgent (`com.user.claude-slack-bridge`). Use `scripts/install.sh` for initial setup.

```bash
# Restart (works from anywhere, including from within the bridge itself)
scripts/restart.sh

# Status
launchctl print gui/$(id -u)/com.user.claude-slack-bridge

# Logs
tail -f ~/Library/Logs/claude-slack-bridge/stderr.log
```

`scripts/restart.sh` は loaded なら `launchctl kickstart -k`、未 load なら `bootstrap` を使う。
kickstart は launchd への指示のみで完結するため、ブリッジ自身のタスクから呼んでも安全
（呼び出し元プロセスツリーが SIGTERM を受けても、再起動は launchd と KeepAlive=true が完遂する）。
`bootout && bootstrap` の chain は自殺で右辺が走らないため、自己再起動には使ってはいけない。

Do NOT start bridge.py directly with `python bridge.py` — always use launchctl.

Configuration is in `.env` (copy from `.env.example`). Required: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ADMIN_SLACK_USER_ID`. Optional: `SLACK_ALLOWED_USERS`, `SLACK_ALLOWED_CHANNELS`, `NOTIFICATION_CHANNEL`, `CLAUDE_CMD`, `DEFAULT_ALLOWED_TOOLS`, `LOG_LEVEL`, `SLACK_LANGUAGE`.

## Architecture

Main logic is in `bridge.py`. Internationalization is in `i18n.py` (bilingual ja/en message table, controlled by `SLACK_LANGUAGE` env var). Tests are in `tests/`.

### Data Model (3-Layer Hierarchy)

- **`Project`** (= Slack Channel) — Session container. Created automatically when a task starts. `ClaudeCodeRunner.projects` dict maps `channel_id → Project`.

- **`Session`** (= Slack Thread) — Created automatically when a task starts. Contains a serial chain of tasks. Holds `claude_session_id` (for `--resume`), label (emoji + name), `working_dir` (required, set at task creation), and `next_tools` (one-shot tool overrides). Sessions are persisted to `sessions.json` for `--resume` restoration after bridge restart.

- **`Task`** (= Single Instruction → Completion) — One Claude Code subprocess invocation. Holds process handle, PTY master fd, tool call history, and `user_id`. Tasks within a session run serially. Thread replies automatically create new tasks with `--resume`.

### Key Components

- **`ClaudeCodeRunner`** — Core manager. `projects` dict maps channels to projects. `get_or_create_project(channel_id)` creates projects on demand. Manages directory history per channel. `run_task(project, session, task)` starts a daemon thread. `_execute(project, session, task)` uses `session.working_dir` as cwd.

- **Access control** — `_is_user_allowed()` and `_is_channel_allowed()` check whitelists. `ADMIN_SLACK_USER_ID` is always allowed. `*` means allow all.

- **Slack event handler** — `handle_message` is the main event handler. Routes channel events (whitelisted, mention required for top-level). Thread reply routing: (1) `instance_threads` registered → mention=command (`cancel`/`cancel <instr>`/`status`/`tools`), otherwise forward to CLI stdin (or, for a running PTY task, queue as a follow-up — see "Mid-task follow-up instructions"), (1.5) `pending_directory_requests` / `pending_fork_selections` / `pending_bind_selections` → selection handling, (2) session exists → mention=command, otherwise auto-resume via new task (queued instead if a task is still running), (3) fallback to command (mention required). `_dispatch_command` is the command parser. `handle_mention` (`app_mention` event) is a no-op to avoid duplicate processing.

- **Notifications** — `NOTIFICATION_CHANNEL` (optional) receives startup/shutdown notifications. If unset, these are logged only.

- **fork** — `detect_running_claude_instances()` finds existing `claude` CLI processes on the Mac. `fork <PID>` integrates a running process into the Project+Session model with I/O forwarding while alive and `--resume` continuation after death.

- **bind** — Live bidirectional connection to a terminal Claude CLI process. `bind <PID>` creates a Slack thread that mirrors the terminal session. Output is monitored via JSONL. Slack thread replies are forwarded to the terminal via AppleScript clipboard+paste. When the bound process dies, the session transitions to `--resume` mode. Selection state is stored in `pending_bind_selections`.

- **root** — `root <path>` sets a channel's default working directory (persisted in `channel_roots.json`). When set, bare tasks (`@bot <task>`) run immediately in that directory without the selection UI. `root` shows the current setting, `root clear` removes it. Handled by `_handle_root`.

- **External takeover** — When a bridge-spawned task is running and an external `claude --resume` process is detected with the same `session_id`, the bridge kills its subprocess, switches the `inst` to the external process, and continues monitoring. Active task takeover is checked every ~15s inside `_monitor_session_jsonl` (via `_TAKEOVER_CHECK_INTERVAL`). Idle session takeover (for sessions with no active task) is detected by a separate thread `_idle_takeover_monitor_loop` (via `_IDLE_TAKEOVER_INTERVAL`). Note: Slack thread displays a truncated session ID (first 12 chars) for reference only — to resume from terminal, use `claude --resume` without args to open the interactive session picker.

- **GitHub update check** — A background thread (`_github_update_monitor_loop`, started in `main()` when `GITHUB_UPDATE_CHECK` is true; **default false in this fork** to avoid applying unreviewed upstream code) periodically compares the local version against GitHub (`GITHUB_UPDATE_INTERVAL`s, default 1h). `_check_github_updates()` detects updates: in a git clone via `git fetch` + `HEAD..origin/<branch>` count; in a non-git install via the GitHub API (`commits/<branch>`) compared to an `installed_sha` baseline in `github_update_state.json` (recorded on first run, no notification then). On a new update it posts a "アップデートがあります" message with an **Apply** button to `NOTIFICATION_CHANNEL` (dedup via in-memory `_github_last_notified_sha`). The `apply_update` action (`handle_apply_update`) runs `_apply_update`: git clone → `git pull --ff-only`; non-git → download the branch tarball and overwrite the tree via `_copy_tree_preserving` (skips `_UPDATE_PROTECTED`: `.env`, state `*.json`, `venv`, `.git`). On success it posts "適用しました" and restarts via `scripts/restart.sh`. Config: `GITHUB_UPDATE_CHECK`, `GITHUB_UPDATE_INTERVAL`, `GITHUB_UPDATE_REPO`, `GITHUB_UPDATE_BRANCH`, optional `GITHUB_TOKEN`. Notifications require `NOTIFICATION_CHANNEL`.

- **team** — `team [in <path>] <task>` runs a task using Claude Code's Team Agent feature. `_handle_team` injects a team instruction prefix into the prompt and adds Team-related tools (`TeamCreate`, `TeamDelete`, `SendMessage`, `TaskCreate`, `TaskUpdate`, `TaskList`, `Agent`) to `allowedTools`. The Team Lead runs as a normal task in the session; teammates are subagents whose JSONL output is monitored by the existing subagent monitoring (`_read_subagent_jsonl_entries`). Teammate progress is displayed with `:busts_in_silhouette:` labels showing their name and type.

- **Bare task** — When `@bot <task>` is sent without `in` or `fork`, `_handle_bare_task` shows fork candidates and directory history for selection. Selection state is stored in `pending_directory_requests`.

- **App Home dashboard** — The Bot's App Home tab shows a cross-channel session dashboard (`_build_home_view` → `views.publish`). Unit is the **session** (each row shows the latest task's state). Three categories: ① running bridge-managed sessions (`runner.projects` → `active_sessions`), ② running external/terminal Claude processes (`detect_running_claude_instances()`, excluding tracked PIDs and bridge descendants), and ③ finished sessions from `session_history.json` (latest 100, displayed up to `SESSION_HISTORY_DISPLAY`). `_collect_dashboard_sessions()` does the cross-channel collection. A per-viewer **Mine/Everyone toggle** (`home_toggle_scope`) re-publishes filtered by `task.user_id` (App Home has no custom tabs, so this is an in-view toggle). External processes are always shown (no Slack owner) and carry a **fork button** (`home_fork`): it resolves a target channel by matching the process cwd against channel roots / existing session `working_dir` (`_find_channel_for_cwd`); if none matches, it DMs the user a 2-choice (`home_fork_create_channel` via `conversations.create` / `home_fork_use_dm`). `session_history.json` is a thin per-session record written in `_execute`'s `finally` (`record_session_history`), independent of volatile Task objects, so finished sessions survive restarts. Updates: `app_home_opened` publishes immediately; `_schedule_home_refresh()` re-publishes recently-active viewers on task start/finish; `_app_home_poll_loop` re-publishes every `APP_HOME_POLL_INTERVAL`s while any session is running. Requires the Home Tab feature + `app_home_opened` subscription; the fork "create channel" path additionally needs the `channels:manage` (or `groups:write`) scope.

- **Slack upload marker (fork addition)** — Claude can attach files from the working directory to the thread by outputting `[SLACK_UPLOAD:relative/path]` (announced via `prompt_slack_upload_info` in the appended system prompt). `_resolve_upload_markers()` resolves them in `_post_completion` for COMPLETED tasks: realpath must stay inside `working_dir`, `.git/` and `.env*` are refused, max `SLACK_UPLOAD_MAX_FILES` (10) files and `SLACK_UPLOAD_MAX_MB` (default 500) each. Rejected entries are noted in the completion message.

- **Child env (fork addition)** — `_CHILD_ENV_BLOCKLIST` strips `CLAUDECODE`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `GITHUB_TOKEN` from the `claude` subprocess environment. Tool-request approve/reject buttons also check `_is_user_allowed()`. `build_command` always appends `_BRIDGE_DIR_DENY_RULES` (`Read/Edit/Write(//<REPO_DIR>/**)`) to `--disallowedTools`, even when a task overrides `disallowed_tools`, so the child cannot read `.env` or modify the bridge. Residual: `Bash(git *)` can still read arbitrary files (e.g. `git diff --no-index`), so keep the Bash allowlist narrow. Install outside any directory mounted into other agents (e.g. NanoClaw's `~/projects`).

- **Quiet mode (fork addition)** — `SLACK_QUIET_MODE` (default true in this fork). For bridge-run tasks, `_flush_progress` posts nothing (bind live mirroring is unaffected; questions/plan approvals still post), the completion message is just `label + elapsed` + Claude's final answer (no tools/tokens/cost header, no session footer, no progress/diff attachments), and `prompt_quiet_style` asks Claude for a short conclusion-first reply. Set `SLACK_QUIET_MODE=false` for upstream behavior.

### Data Flow

1. Message event arrives via Socket Mode → `handle_message` routes by `channel_type`
2. Channel/user whitelist check → mention detection / thread reply routing → command parsing
3. `_dispatch_command` parses the command. `in` → `_handle_in_dir` → `_start_task_in_dir`. `team` → `_handle_team` (injects team prompt prefix + extra tools, then starts task). `fork` → `_handle_fork` / `_handle_fork_list`. `bind` → `_handle_bind` / `_handle_bind_list` → `_execute_bind`. `root` → `_handle_root` (persists to `channel_roots.json`). Bare task → `_handle_bare_task` → directory selection → `_start_task_in_dir` or `_execute_fork`.
4. Thread spawns `claude -p --verbose` subprocess with PTY, pipes prompt via stdin. Thread replies to existing sessions automatically use `--resume <session.claude_session_id>`
5. JSONL output file is monitored for progress (including subagent JSONL files under `subagents/`); session's `claude_session_id` is updated from JSONL entries
6. On completion/failure, posts final result to Slack thread

### Session Continuity

Thread replies to a session's Slack thread automatically create new tasks with `--resume <session.claude_session_id>`. The old `continue` and `resume` commands are deprecated. Each session maintains its own `claude_session_id`, label, and working directory.

### Mid-task follow-up instructions (pending_followup)

Replies sent **while a task is still running** are no longer rejected — they are queued and auto-fired as a `--resume` task after the current one ends (matching the interactive CLI's "type while it's working, processed at the next turn" UX). Two modes:

- **Queue (default)** — a plain reply during a running task appends to `session.pending_followup` (`mode="queue"`) and the current task runs to completion first. Cheapest (no wasted work, tool set unchanged → prompt cache preserved).
- **Cancel (`@bot cancel <instruction>`)** — appends with `mode="cancel"` then immediately cancels the running task; on fire, the prompt is prefixed with `followup_cancel_prefix` so the resumed Claude knows it was interrupted. Equivalent to Esc-then-retype in the CLI.

Firing happens in `_execute`'s `finally` via `ClaudeCodeRunner._fire_one_followup` — **after** teardown (`instance_threads` removal, `clear_active_task`) and **after** `session.claude_session_id` is settled (the JSONL fallback at the end of `_execute` runs first). This structurally avoids the same-session/JSONL race and removes any need for a timeout wait on the session id. Only one item is popped per `finally`; the fired task's own `finally` drains the next, giving FIFO serial execution. Skipped when the session is in bind/takeover mode (`is_takeover`). `@bot cancel` with no argument also clears the queue. Enqueue entry points: the reject branch of `_handle_instance_input` (PTY tasks) and an `active_task`-running guard in `handle_message`'s session path (covers the stdin-fallback path that isn't registered in `instance_threads`). `_enqueue_followup` builds the queue entries; `status` shows the per-session queued count.

### Persistence

- `directory_history.json` — Per-channel directory usage history (channel_id → [dir_path, ...]). Max 10 entries per channel.
- `sessions.json` — Per-channel session data (claude_session_id, working_dir, label, etc.). Restored on bridge restart for `--resume` continuity. Unloaded channels' data is preserved across saves. No age expiry or per-channel count cap: as long as the underlying `claude_session_id` JSONL (`~/.claude/projects/*/<id>.jsonl`) survives on the Claude CLI side, `--resume` still works, so the bridge must not forget a still-resumable session. Entries are tiny (a few hundred bytes) and thread count is naturally bounded by usage.
- `channel_roots.json` — Per-channel root directory settings (channel_id → path). Used by `root` command. Bare tasks in channels with a root set run immediately in that directory.
- `session_history.json` — Thin records of finished sessions for the App Home dashboard (latest task's status/prompt/dir/user/timestamps/tool_count per thread). Global cap of 100 (`SESSION_HISTORY_MAX`). Written on session completion; survives restart (Task objects do not).
- Tasks are volatile (in-memory only, lost on bridge restart).

## Language

The codebase, comments, and Slack messages are in Japanese. Documentation (README, setup guide) is bilingual (English + Japanese). Maintain Japanese for user-facing Slack messages and code comments. Tests exist in `tests/`.
