"""
i18n — 環境変数 SLACK_LANGUAGE による日本語/英語切り替え。
デフォルトは "ja"。未知のキーは ja → キーそのまま でフォールバック。
"""

import os

MESSAGES: dict[str, dict[str, str]] = {
    "ja": {
        # ── help ──
        "help_text": (
            ":robot_face: *Claude Code Bridge* — 使い方:\n"
            "*基本操作:*\n"
            "• `@bot in <path> <タスク>` → 指定ディレクトリでタスクを実行\n"
            "• `@bot fork [<PID|リンク|ラベル|セッションID>] [<タスク>]` → セッションを分岐\n"
            "• `@bot fork` → フォーク可能な候補一覧\n"
            "• `@bot bind <PID>` → ターミナルのclaude CLIにライブ接続\n"
            "• `@bot bind` → バインド可能なプロセス一覧\n"
            "• `@bot <タスク>` → ディレクトリ選択画面から実行\n"
            "• `@bot team in <path> <タスク>` → Team Agentで並列実行\n"
            "• `@bot team <タスク>` → Team Agentで並列実行（root設定時）\n"
            "*スレッド返信:*\n"
            "• `<指示>` → 同セッションで自動続行（メンション不要）\n"
            "• 実行中に送ると → 完了後に続けて実行（キュー）\n"
            "• `@bot cancel <指示>` → 実行中を中断して追加指示を即実行\n"
            "• `@bot cancel` → このスレッドのタスクをキャンセル（キューも破棄）\n"
            "• `@bot status` → タスクの状態一覧\n"
            "• `@bot tools <tool1,...>` → 次回の許可ツール設定\n"
            "*管理:*\n"
            "• `@bot status` → タスクの状態一覧\n"
            "• `@bot sessions` → セッション一覧\n"
            "• `@bot cancel` → 実行中のタスクをキャンセル\n"
            "*設定:*\n"
            "• `@bot root <絶対パス>` → チャンネルのルートディレクトリを設定\n"
            "• `@bot root` → 現在のルートディレクトリを表示\n"
            "• `@bot root clear` → ルートディレクトリを解除"
        ),

        # ── status ──
        "status_thinking": ":thought_balloon: _思考中..._ ({chars}文字)",
        "status_running": ":hourglass_flowing_sand: _実行中..._",
        "status_last_update": "最終更新 {idle}前",
        "status_no_tasks": ":zzz: このチャンネルにはまだタスクがありません",
        "status_no_running_tasks": ":zzz: 実行中のタスクはありません",
        "status_no_running_with_recent": ":zzz: 実行中のタスクはありません\n*直近の完了タスク:*",
        "status_running_tasks_header": ":gear: *実行中のタスク ({count}セッション)*",
        "status_elapsed_seconds": "({elapsed:.0f}秒)",
        "status_elapsed_tools": "({elapsed:.0f}秒, ツール{tool_count}回)",
        "status_recent_tools": "最近: {tools}",
        "status_followup_queued": ":inbox_tray: 追加指示 {count} 件待機中（完了後に実行）",
        "status_continued": "...(続き)",

        # ── task ──
        "task_complete": ":white_check_mark: *タスク完了* ({elapsed:.0f}秒)",
        "task_cancelled": ":stop_sign: キャンセルされました",
        "task_failed": ":x: 失敗 (exit {code})",
        "task_interrupted_by_restart": (
            ":electric_plug: {label}  ネットワーク切断などで bridge が再起動され、"
            "このタスクは中断されました。\n"
            "_直前の指示_: {prompt}\n"
            "このスレッドに返信すると `--resume` で続きから再開します。"
        ),
        "task_error": ":x: エラー: {error}",
        "task_last_action": ":rewind: 最後の操作: `{tool}` {summary}",
        "task_tools_used": "使用ツール ({count}回): {tools}",
        "task_tools_more": " 他{count}件",
        "task_tokens": ":bar_chart: トークン: 入力 {input} / 出力 {output} (キャッシュ読取 {cache_read})",
        "task_cost_estimate": "推定コスト: ${cost:.4f}",
        "task_see_full_file": "\n...(全文はファイルを参照)",
        "task_full_text_title": "全文",
        "status_history_title": "ステータス履歴",
        "task_reply_to_continue": "_このスレッドに返信すると自動で続行します_",
        "task_working_dir_not_set": "作業ディレクトリが未設定です",
        "task_cancel_request_sent": ":stop_sign: タスク #{task_id} のキャンセルリクエストを送信しました",
        "task_not_running": "タスク #{task_id} は実行中ではありません",
        "task_timeout_header": ":warning: PID {pid} _(タイムアウト — 部分的な応答)_",

        # ── error ──
        "error_cancel_no_active": ":warning: 実行中のタスクがありません",
        "error_tools_thread_only": ":warning: `tools` コマンドはスレッド内でのみ有効です。タスクのスレッドに返信してください。",
        "error_continue_deprecated": ":warning: `continue` / `resume` は廃止されました。タスクのスレッドに返信すると自動で続行します。",
        "error_absolute_path_required": ":warning: 絶対パスで指定してください: `{path}`",
        "error_dir_not_found": ":warning: ディレクトリが見つかりません: `{path}`",
        "error_in_usage": ":warning: 使い方: `in <path> タスク内容`\n絶対パスで指定してください（`~` 展開あり）",
        "error_absolute_path_with_root_hint": (
            ":warning: 絶対パスで指定してください: `{path}`\n"
            "\U0001f4a1 `root <絶対パス>` でルートディレクトリを設定すると相対パスが使えます"
        ),
        "error_pid_not_number": ":warning: PIDは数字で指定してください: `fork <PID> [<task>]`",
        "error_enter_number_range": ":warning: 1〜{max} の番号を入力してください",
        "error_pid_no_tty": ":warning: PID {pid} にはTTYが接続されていません",
        "error_input_send_failed": ":x: 入力送信エラー: {error}",
        "error_need_working_dir": (
            ":warning: 作業ディレクトリを指定してください\n"
            "• `in <path> <タスク>` — 指定ディレクトリで実行\n"
            "• `fork <PID|ラベル>` — セッションをフォーク"
        ),
        "error_enter_number_path_cancel": ":warning: 番号、絶対パス、または `cancel` を入力してください",

        # ── fork ──
        "fork_no_instances": ":mag: 実行中のclaude CLIインスタンスが見つかりません",
        "fork_pid_already_tracked": ":warning: PID {pid} は既に追跡中です",
        "fork_pid_not_found": ":warning: PID {pid} が見つかりません",
        "fork_no_forkable": ":mag: フォーク可能な候補がありません（外部プロセス・セッションとも）",
        "fork_list_header": ":fork_and_knife: *フォーク可能な候補:*",
        "fork_ext_header": "\n:computer: *外部プロセス:*",
        "fork_session_header": "\n:clipboard: *ブリッジセッション:*",
        "fork_select_or_cancel": "\n番号を入力して選択、または `cancel` でキャンセル",
        "fork_cancelled": ":x: フォーク選択をキャンセルしました",
        "fork_pid_exited": ":warning: PID {pid} は既に終了しています",
        "fork_session_id_not_found": (
            ":x: PID {pid} の session_id を取得できませんでした\n"
            ":file_folder: `{cwd}`\n"
            "JSONLファイルが見つからないか、session_id が含まれていません"
        ),
        "fork_session_not_found": ":warning: `{query}` に一致するフォーク可能なセッションが見つかりません",
        "fork_session_matches": ":fork_and_knife: `{query}` に一致するセッション:",
        "fork_session_start": (
            ":fork_and_knife: セッション *{source}* から分岐しました\n"
            ":file_folder: `{cwd}`\n"
            "_元のセッションには影響しません。このスレッドに返信してタスクを開始してください_"
        ),

        # ── bind ──
        "bind_no_instances": ":mag: 実行中のclaude CLIインスタンスが見つかりません",
        "bind_pid_already_tracked": ":warning: PID {pid} は既に追跡中です",
        "bind_pid_not_found": ":warning: PID {pid} が見つかりません",
        "bind_no_bindable": ":mag: バインド可能なclaude CLIインスタンスはありません",
        "bind_list_header": ":computer: *バインド可能なclaude CLIインスタンス:*",
        "bind_select_or_cancel": "\n番号を入力して選択、または `cancel` でキャンセル",
        "bind_cancelled": ":x: バインド選択をキャンセルしました",
        "bind_pid_exited": ":warning: PID {pid} は既に終了しています",
        "bind_start": (
            ":link: PID {pid} にバインドしました（ライブI/O）\n"
            ":file_folder: `{cwd}`{sid_info}\n"
            "_ターミナルの出力がこのスレッドに表示されます。返信で入力を転送できます_"
        ),
        "bind_free_input_sent": ":arrow_right: PID {pid} にテキストを送信しました",
        "bind_session_takeover": (
            ":arrows_counterclockwise: ターミナル (PID {pid}) がセッションを引き継ぎました\n"
            "_このスレッドへの返信はターミナルに転送されます_"
        ),
        "bind_session_takeover_ended": (
            ":stop_button: ターミナル (PID {pid}) のセッションが終了しました\n"
            "_このスレッドに返信すると --resume で続行します_"
        ),
        "external_takeover": (
            ":desktop_computer: ターミナル (PID {pid}) がこのセッションを引き継ぎました\n"
            "_Slack側のタスクは中断されました。ターミナルでの操作が完了するまでお待ちください。\n"
            "ターミナル終了後、このスレッドに返信すると --resume で続行できます_"
        ),
        "external_takeover_ended": (
            ":white_check_mark: ターミナル (PID {pid}) でのセッションが終了しました\n"
            "_このスレッドに返信すると --resume で続行できます_"
        ),

        # ── session ──
        "session_no_history": "セッション履歴はまだありません",
        "session_list_header": ":clipboard: *セッション一覧*",
        "session_task_count": "({count}タスク)",
        "session_reply_to_continue": "\n_タスクのスレッドに返信すると自動で続行します_",
        "session_pid_exited": ":stop_button: PID {pid} が終了しました",

        # ── dir (ディレクトリ選択) ──
        "dir_select_header": ":file_folder: *作業ディレクトリを選択してください:*",
        "dir_forkable_header": "\n:computer: *フォーク可能なプロセス:*",
        "dir_recent_header": "\n:clock1: *最近のディレクトリ:*",
        "dir_select_prompt": "\n番号で選択、絶対パスを入力、または `cancel` でキャンセル",
        "dir_cancelled": ":x: キャンセルしました",

        # ── input (入力転送) ──
        "input_sent": ":arrow_right: PID {pid} に入力を送信しました :white_check_mark:",
        "input_answer_sent": ":arrow_right: PID {pid} に回答を送信: {label} :white_check_mark:",
        "input_selected_option": "選択肢 {num} ({label}) を選択",
        "input_blocked_task_running": ":hourglass_flowing_sand: タスク実行中のため、新しい指示は受け付けられません。完了後にもう一度返信してください。",

        # ── followup (実行中の追加指示キュー) ──
        "followup_queued": ":inbox_tray: 追加指示を受け付けました（実行中タスクの完了後に続けて実行します・待機 {count} 件）",
        "followup_queued_cancel": ":fast_forward: 実行中タスクを中断して、追加指示を反映します",
        "followup_firing": ":arrow_forward: 追加指示を実行します: {instruction}",
        "followup_cancel_prefix": "（前のタスクは新しい指示のため中断されました。これまでの作業内容を踏まえて、続けて次の指示に対応してください）\n\n追加指示: ",
        "followup_dropped": "（キュー済みの追加指示 {count} 件も破棄しました）",

        # ── notify (起動/停止通知) ──
        "notify_startup": (
            ":rocket: *Claude Code Bridge が起動しました*\n"
            "チャンネルで `@bot in <path> <タスク>` を送信してください"
        ),
        "notify_shutdown": ":wave: Claude Code Bridge を停止しました",
        # ── update (GitHub 更新通知) ──
        "update_available": (
            ":sparkles: *アップデートがあります*\n"
            "新しいバージョンが公開されています。下のボタンで適用できます。"
        ),
        "update_apply_button": "アップデートを適用",
        "update_applying": ":hourglass_flowing_sand: アップデートを適用しています…",
        "update_done": ":white_check_mark: アップデートを適用しました。再起動します。",
        "update_failed": (
            ":warning: 自動アップデートに失敗しました（{reason}）。\n"
            "お手数ですが手動で更新してください。"
        ),
        "notify_missing_scopes": (
            ":warning: *Slack Bot の権限が不足しています*\n"
            "未付与のスコープ: `{scopes}`\n"
            "Slack App 設定の *OAuth & Permissions* で上記スコープを追加し、"
            "ワークスペースに再インストール（再認可）してください。\n"
            "このままだと一部機能（タスク進捗リアクションなど）が動作しません。"
        ),

        # ── question (CLI質問表示) ──
        "question_cli_header": ":question: *CLIからの質問*",
        "question_reply_with_number": "_番号を返信してください（テキストでOther回答も可）_",
        "question_multi_select_unsupported": "_複数選択が必要ですが、現在は未対応です。テキストで回答してください。_",
        "question_plan_approval_required": ":clipboard: *プランの承認が必要です*",
        "question_plan_truncated": "...(省略)",
        "question_approve_execute": "承認して実行",
        "question_reject_feedback": "却下・フィードバック",
        "question_reply_with_feedback": "_番号を返信してください（テキストでフィードバックも可）_",
        "question_allowed_prompts": "*許可プロンプト:*",
        "question_input_required": "入力が必要です",

        # ── root (ルートディレクトリ) ──
        "root_current": ":file_folder: このチャンネルのルートディレクトリ: `{path}`",
        "root_not_set": ":file_folder: このチャンネルにルートディレクトリは設定されていません\n`root <絶対パス>` で設定できます",
        "root_cleared": ":wastebasket: ルートディレクトリを解除しました（旧: `{old}`）",
        "root_already_not_set": ":file_folder: ルートディレクトリは設定されていません",
        "root_set": ":white_check_mark: ルートディレクトリを設定しました: `{path}`\n以降 `@bot <タスク>` で即座に実行されます",
        "root_dir_not_found": (
            ":warning: ルートディレクトリが見つかりません: `{path}`\n"
            "`root <絶対パス>` で再設定するか `root clear` で解除してください"
        ),

        # ── team ──
        "team_usage": ":warning: 使い方: `team in <path> <タスク>` または `team <タスク>`（root設定時）",
        "team_prompt_prefix": (
            "このタスクはTeam Agent機能を使って並列に実行してください。\n"
            "1. TeamCreateでチームを作成\n"
            "2. タスクを分析して並列実行可能なサブタスクに分解\n"
            "3. 各サブタスクに適切なsubagent_type（Explore=調査, general-purpose=実装, Plan=設計）のチームメイトをAgentツールで生成\n"
            "4. TaskCreate/TaskUpdateでタスク管理し、完了後にTeamDeleteでクリーンアップ\n"
            "\n実際のタスク:\n"
        ),
        "team_subagent_label": ":busts_in_silhouette: `{name}` ({agent_type}) {desc}",

        # ── tools ──
        "tools_set": ":wrench: このセッションの次のタスクの許可ツール: `{tools}`\n続けてタスクを送信してください",
        "tool_request_message": ":wrench: 以下のツールの実行許可が必要です:\n{tools}",
        "tool_request_message_suffix": "下のボタンで応答してください。一言コメントを添えるとClaudeに伝わります（却下時の理由付けにも使えます）。",
        "tool_request_context": ":file_folder: `{dir}`",
        "tool_request_comment_label": "一言添える（任意）",
        "tool_request_approve_once": "一回のみ許可",
        "tool_request_approve_session": "セッション中許可",
        "tool_request_approve_project": "プロジェクト中許可",
        "tool_request_reject": "キャンセル",
        "tool_request_approved_once": ":white_check_mark: ツール {tools} を許可して再実行します（一回のみ）",
        "tool_request_approved_session": ":white_check_mark: ツール {tools} をこのセッション中許可して再実行します",
        "tool_request_approved_project": ":white_check_mark: ツール {tools} をこのプロジェクト中許可して再実行します",
        "tool_request_rejected": ":x: ツール許可リクエストをキャンセルしました",
        "tool_request_user_comment": "ユーザーから一言: {comment}",
        "tool_request_rejected_with_comment": ":x: ツール許可リクエストをキャンセルしました（コメントをClaudeに伝えます）",

        # ── prompt (Claude向けプロンプト) ──
        "prompt_attached_files": "添付ファイル:",
        "prompt_answer_to_question": "以下の質問への回答です:\n質問: {question}",
        "prompt_answer_to_prev": "前の質問への回答です:",
        "prompt_options_label": "選択肢:",
        "prompt_answer_numbered": "回答: {num}. {label}",
        "prompt_answer_text": "回答: {label}",
        "prompt_system_append": (
            "ユーザーに質問や確認が必要な場合は、AskUserQuestionツールを使わず、"
            "テキストで質問を出力してください。"
            "質問を出力したら、その場で応答を終了してください。"
            "ユーザーは次のメッセージで回答します。"
            "質問の回答を待たずに処理を進めないでください。"
            "プランモードを終了する場合は、ExitPlanModeツールを使わず、"
            "プラン内容をテキストで出力してください。"
            "ユーザーがプランを承認した後、次のメッセージで実行を開始します。"
            "ツールの呼び出しが権限不足で失敗・拒否された場合や、"
            "allowedToolsに含まれないツールが必要な場合は、"
            "代替案の提示や手動実行の提案をせず、"
            "応答の末尾に [TOOL_REQUEST:ツール名] の形式でマーカーを必ず出力してください。"
            "ユーザーが許可すればそのツールを使って自動的に再実行されます。"
            "例: [TOOL_REQUEST:Bash(npm install)] や [TOOL_REQUEST:WebFetch]。"
            "複数のツールが必要な場合は複数のマーカーを出力してください。"
        ),
        "prompt_allowed_tools_info": (
            "\n現在あなたに許可されているツール: {tools}。"
            "これ以外のツールが必要な場合（例: 許可リストにないBashコマンドの実行、"
            "WebFetchによるダウンロードなど）は、手動実行を提案するのではなく、"
            "必ず [TOOL_REQUEST:ツール名] マーカーを出力してください。"
        ),

        # ── App Home ダッシュボード ──
        "home_title": ":satellite_antenna: Claude セッションダッシュボード",
        "home_subtitle": "表示: {scope}  ·  更新 {updated}",
        "home_scope_mine": "自分のみ",
        "home_scope_all": "全員",
        "home_toggle_to_all": ":busts_in_silhouette: 全員を表示",
        "home_toggle_to_mine": ":bust_in_silhouette: 自分のみ表示",
        "home_refresh_button": ":arrows_counterclockwise: 更新",
        "home_section_running": ":gear: *進行中（ブリッジ管理） — {count}*",
        "home_section_external": ":computer: *進行中（ターミナル / 外部） — {count}*",
        "home_section_finished": ":checkered_flag: *終了済み — {count}*",
        "home_none": "_なし_",
        "home_more": "_他 {count} 件_",
        "home_open_thread": ":speech_balloon: スレッドを開く",
        "home_truncated": "_…表示上限に達したため一部を省略しました_",
        "home_overview": ":open_file_folder: {groups} ディレクトリ  ·  進行中 {running}  ·  終了 {finished}",
        "home_group_summary": "_(進行中 {running} · 終了 {finished})_",
        "home_group_unknown": "（場所不明）",
        "home_ext_line": ":computer: 外部プロセス PID `{pid}`  :clock1: {etime}",
        "home_fork_button": "fork",
        "home_not_allowed": ":lock: このダッシュボードを表示する権限がありません。",
        "home_dur_sec": "{n}秒",
        "home_dur_min": "{n}分",
        "home_dur_hour": "{n}時間{m}分",
        "home_fork_thread_header": ":fork_and_knife: 外部セッションを取り込みました（cwd: `{cwd}`）。このスレッドに返信すると続きから実行します。",
        "home_fork_failed": ":x: fork に失敗しました。",
        "home_fork_no_channel": (
            ":mag: cwd `{cwd}` に対応するチャンネルが見つかりませんでした。\n"
            "取り込み先を選んでください。"
        ),
        "home_fork_create_channel": ":heavy_plus_sign: チャンネルを作成",
        "home_fork_use_dm": ":speech_balloon: このDMで開く",
        "home_fork_creating": ":hourglass_flowing_sand: チャンネルを作成しています…",
        "home_fork_opening_dm": ":hourglass_flowing_sand: DMで開いています…",
    },
    "en": {
        # ── help ──
        "help_text": (
            ":robot_face: *Claude Code Bridge* — Usage:\n"
            "*Basic:*\n"
            "• `@bot in <path> <task>` → Run a task in the specified directory\n"
            "• `@bot fork [<PID|link|label|session-ID>] [<task>]` → Fork a session\n"
            "• `@bot fork` → List forkable candidates\n"
            "• `@bot bind <PID>` → Live-connect to a terminal Claude CLI\n"
            "• `@bot bind` → List bindable processes\n"
            "• `@bot <task>` → Run from directory selection\n"
            "• `@bot team in <path> <task>` → Run with Team Agent (parallel)\n"
            "• `@bot team <task>` → Run with Team Agent (when root is set)\n"
            "*Thread replies:*\n"
            "• `<instruction>` → Continue in same session (no mention needed)\n"
            "• Sent while running → queued, runs after the current task finishes\n"
            "• `@bot cancel <instruction>` → Interrupt the running task and run it now\n"
            "• `@bot cancel` → Cancel this thread's task (also drops the queue)\n"
            "• `@bot status` → Show task status\n"
            "• `@bot tools <tool1,...>` → Set allowed tools for next task\n"
            "*Management:*\n"
            "• `@bot status` → Show task status\n"
            "• `@bot sessions` → Show session list\n"
            "• `@bot cancel` → Cancel a running task\n"
            "*Settings:*\n"
            "• `@bot root <absolute-path>` → Set channel root directory\n"
            "• `@bot root` → Show current root directory\n"
            "• `@bot root clear` → Clear root directory"
        ),

        # ── status ──
        "status_thinking": ":thought_balloon: _Thinking..._ ({chars} chars)",
        "status_running": ":hourglass_flowing_sand: _Running..._",
        "status_last_update": "last update {idle} ago",
        "status_no_tasks": ":zzz: No tasks in this channel yet",
        "status_no_running_tasks": ":zzz: No running tasks",
        "status_no_running_with_recent": ":zzz: No running tasks\n*Recent completed tasks:*",
        "status_running_tasks_header": ":gear: *Running tasks ({count} sessions)*",
        "status_elapsed_seconds": "({elapsed:.0f}s)",
        "status_elapsed_tools": "({elapsed:.0f}s, {tool_count} tool calls)",
        "status_recent_tools": "Recent: {tools}",
        "status_followup_queued": ":inbox_tray: {count} queued instruction(s) (run after completion)",
        "status_continued": "...(continued)",

        # ── task ──
        "task_complete": ":white_check_mark: *Task completed* ({elapsed:.0f}s)",
        "task_cancelled": ":stop_sign: Cancelled",
        "task_failed": ":x: Failed (exit {code})",
        "task_interrupted_by_restart": (
            ":electric_plug: {label}  Bridge restarted (likely due to a network outage) "
            "and this task was interrupted.\n"
            "_Last prompt_: {prompt}\n"
            "Reply in this thread to resume with `--resume`."
        ),
        "task_error": ":x: Error: {error}",
        "task_last_action": ":rewind: Last action: `{tool}` {summary}",
        "task_tools_used": "Tools used ({count}): {tools}",
        "task_tools_more": " +{count} more",
        "task_tokens": ":bar_chart: Tokens: in {input} / out {output} (cache read {cache_read})",
        "task_cost_estimate": "Est. cost: ${cost:.4f}",
        "task_see_full_file": "\n...(see full file)",
        "task_full_text_title": "Full text",
        "status_history_title": "Status history",
        "task_reply_to_continue": "_Reply to this thread to continue automatically_",
        "task_working_dir_not_set": "Working directory not set",
        "task_cancel_request_sent": ":stop_sign: Cancel request sent for task #{task_id}",
        "task_not_running": "Task #{task_id} is not running",
        "task_timeout_header": ":warning: PID {pid} _(timeout — partial response)_",

        # ── error ──
        "error_cancel_no_active": ":warning: No running tasks",
        "error_tools_thread_only": ":warning: The `tools` command is only available in threads. Reply in the task's thread.",
        "error_continue_deprecated": ":warning: `continue` / `resume` are deprecated. Reply to the task's thread to continue automatically.",
        "error_absolute_path_required": ":warning: Please use an absolute path: `{path}`",
        "error_dir_not_found": ":warning: Directory not found: `{path}`",
        "error_in_usage": ":warning: Usage: `in <path> task description`\nPlease use an absolute path (`~` expansion supported)",
        "error_absolute_path_with_root_hint": (
            ":warning: Please use an absolute path: `{path}`\n"
            "\U0001f4a1 Set a root directory with `root <absolute-path>` to use relative paths"
        ),
        "error_pid_not_number": ":warning: PID must be a number: `fork <PID> [<task>]`",
        "error_enter_number_range": ":warning: Please enter a number from 1 to {max}",
        "error_pid_no_tty": ":warning: PID {pid} has no TTY attached",
        "error_input_send_failed": ":x: Input send error: {error}",
        "error_need_working_dir": (
            ":warning: Please specify a working directory\n"
            "• `in <path> <task>` — Run in specified directory\n"
            "• `fork <PID|label>` — Fork a session"
        ),
        "error_enter_number_path_cancel": ":warning: Please enter a number, absolute path, or `cancel`",

        # ── fork ──
        "fork_no_instances": ":mag: No running Claude CLI instances found",
        "fork_pid_already_tracked": ":warning: PID {pid} is already being tracked",
        "fork_pid_not_found": ":warning: PID {pid} not found",
        "fork_no_forkable": ":mag: No forkable candidates available (no external processes or sessions)",
        "fork_list_header": ":fork_and_knife: *Forkable candidates:*",
        "fork_ext_header": "\n:computer: *External processes:*",
        "fork_session_header": "\n:clipboard: *Bridge sessions:*",
        "fork_select_or_cancel": "\nEnter a number to select, or `cancel` to abort",
        "fork_cancelled": ":x: Fork selection cancelled",
        "fork_pid_exited": ":warning: PID {pid} has already exited",
        "fork_session_id_not_found": (
            ":x: Could not retrieve session_id for PID {pid}\n"
            ":file_folder: `{cwd}`\n"
            "JSONL file not found or does not contain session_id"
        ),
        "fork_session_not_found": ":warning: No forkable session matching `{query}` found",
        "fork_session_matches": ":fork_and_knife: Sessions matching `{query}`:",
        "fork_session_start": (
            ":fork_and_knife: Forked from session *{source}*\n"
            ":file_folder: `{cwd}`\n"
            "_The original session will not be affected. Reply to this thread to start a task_"
        ),

        # ── bind ──
        "bind_no_instances": ":mag: No running Claude CLI instances found",
        "bind_pid_already_tracked": ":warning: PID {pid} is already being tracked",
        "bind_pid_not_found": ":warning: PID {pid} not found",
        "bind_no_bindable": ":mag: No bindable Claude CLI instances available",
        "bind_list_header": ":computer: *Bindable Claude CLI instances:*",
        "bind_select_or_cancel": "\nEnter a number to select, or `cancel` to abort",
        "bind_cancelled": ":x: Bind selection cancelled",
        "bind_pid_exited": ":warning: PID {pid} has already exited",
        "bind_start": (
            ":link: Bound to PID {pid} (live I/O)\n"
            ":file_folder: `{cwd}`{sid_info}\n"
            "_Terminal output will appear in this thread. Reply to forward input._"
        ),
        "bind_free_input_sent": ":arrow_right: Text sent to PID {pid}",
        "bind_session_takeover": (
            ":arrows_counterclockwise: Terminal (PID {pid}) has taken over the session\n"
            "_Replies to this thread will be forwarded to the terminal_"
        ),
        "bind_session_takeover_ended": (
            ":stop_button: Terminal (PID {pid}) session has ended\n"
            "_Reply to this thread to continue with --resume_"
        ),
        "external_takeover": (
            ":desktop_computer: Terminal (PID {pid}) has taken over this session\n"
            "_The Slack task has been suspended. Please wait until the terminal operation completes.\n"
            "After the terminal exits, reply to this thread to continue with --resume_"
        ),
        "external_takeover_ended": (
            ":white_check_mark: Terminal (PID {pid}) session has ended\n"
            "_Reply to this thread to continue with --resume_"
        ),

        # ── session ──
        "session_no_history": "No session history yet",
        "session_list_header": ":clipboard: *Session list*",
        "session_task_count": "({count} tasks)",
        "session_reply_to_continue": "\n_Reply to a task's thread to continue automatically_",
        "session_pid_exited": ":stop_button: PID {pid} has exited",

        # ── dir ──
        "dir_select_header": ":file_folder: *Select a working directory:*",
        "dir_forkable_header": "\n:computer: *Forkable processes:*",
        "dir_recent_header": "\n:clock1: *Recent directories:*",
        "dir_select_prompt": "\nSelect by number, enter an absolute path, or `cancel` to abort",
        "dir_cancelled": ":x: Cancelled",

        # ── input ──
        "input_sent": ":arrow_right: Input sent to PID {pid} :white_check_mark:",
        "input_answer_sent": ":arrow_right: Answer sent to PID {pid}: {label} :white_check_mark:",
        "input_selected_option": "Option {num} ({label}) selected",
        "input_blocked_task_running": ":hourglass_flowing_sand: A task is currently running. Please reply again after it completes.",

        # ── followup (queued instructions while a task is running) ──
        "followup_queued": ":inbox_tray: Got it — queued to run after the current task finishes ({count} waiting)",
        "followup_queued_cancel": ":fast_forward: Interrupting the running task to apply your new instruction",
        "followup_firing": ":arrow_forward: Running the queued instruction: {instruction}",
        "followup_cancel_prefix": "(The previous task was interrupted in favor of a new instruction. Building on the work done so far, please continue with the following.)\n\nAdditional instruction: ",
        "followup_dropped": "(Also discarded {count} queued instruction(s))",

        # ── notify ──
        "notify_startup": (
            ":rocket: *Claude Code Bridge started*\n"
            "Send `@bot in <path> <task>` in a channel to begin"
        ),
        "notify_shutdown": ":wave: Claude Code Bridge stopped",
        # ── update (GitHub update notification) ──
        "update_available": (
            ":sparkles: *An update is available*\n"
            "A newer version has been released. Apply it with the button below."
        ),
        "update_apply_button": "Apply update",
        "update_applying": ":hourglass_flowing_sand: Applying the update…",
        "update_done": ":white_check_mark: Update applied. Restarting.",
        "update_failed": (
            ":warning: Auto-update failed ({reason}).\n"
            "Please update manually."
        ),
        "notify_missing_scopes": (
            ":warning: *Slack Bot is missing permissions*\n"
            "Scopes not granted: `{scopes}`\n"
            "Add them under *OAuth & Permissions* in your Slack App settings and "
            "reinstall (re-authorize) to the workspace.\n"
            "Until then, some features (e.g. task progress reactions) will not work."
        ),

        # ── question ──
        "question_cli_header": ":question: *Question from CLI*",
        "question_reply_with_number": "_Reply with a number (or text for Other)_",
        "question_multi_select_unsupported": "_Multiple selection is required but not yet supported. Please reply with text._",
        "question_plan_approval_required": ":clipboard: *Plan approval required*",
        "question_plan_truncated": "...(truncated)",
        "question_approve_execute": "Approve and execute",
        "question_reject_feedback": "Reject / Feedback",
        "question_reply_with_feedback": "_Reply with a number (or text for feedback)_",
        "question_allowed_prompts": "*Allowed prompts:*",
        "question_input_required": "Input required",

        # ── root ──
        "root_current": ":file_folder: Channel root directory: `{path}`",
        "root_not_set": ":file_folder: No root directory set for this channel\nSet one with `root <absolute-path>`",
        "root_cleared": ":wastebasket: Root directory cleared (was: `{old}`)",
        "root_already_not_set": ":file_folder: No root directory is set",
        "root_set": ":white_check_mark: Root directory set: `{path}`\nTasks via `@bot <task>` will run here immediately",
        "root_dir_not_found": (
            ":warning: Root directory not found: `{path}`\n"
            "Set a new one with `root <absolute-path>` or clear with `root clear`"
        ),

        # ── team ──
        "team_usage": ":warning: Usage: `team in <path> <task>` or `team <task>` (when root is set)",
        "team_prompt_prefix": (
            "Use the Team Agent feature to execute this task in parallel.\n"
            "1. Create a team with TeamCreate\n"
            "2. Analyze the task and decompose it into parallelizable subtasks\n"
            "3. Spawn teammates with the Agent tool using appropriate subagent_type (Explore=research, general-purpose=implementation, Plan=design)\n"
            "4. Manage tasks with TaskCreate/TaskUpdate, then clean up with TeamDelete when done\n"
            "\nActual task:\n"
        ),
        "team_subagent_label": ":busts_in_silhouette: `{name}` ({agent_type}) {desc}",

        # ── tools ──
        "tools_set": ":wrench: Allowed tools for next task in this session: `{tools}`\nSend a task to continue",
        "tool_request_message": ":wrench: The following tools need permission to run:\n{tools}",
        "tool_request_message_suffix": "Respond with a button below. You can optionally add a comment that will be passed to Claude (useful when cancelling, too).",
        "tool_request_context": ":file_folder: `{dir}`",
        "tool_request_comment_label": "Add a comment (optional)",
        "tool_request_approve_once": "Allow once",
        "tool_request_approve_session": "Allow for session",
        "tool_request_approve_project": "Allow for project",
        "tool_request_reject": "Cancel",
        "tool_request_approved_once": ":white_check_mark: Approved {tools} — retrying (once only)",
        "tool_request_approved_session": ":white_check_mark: Approved {tools} for this session — retrying",
        "tool_request_approved_project": ":white_check_mark: Approved {tools} for this project — retrying",
        "tool_request_rejected": ":x: Tool permission request cancelled",
        "tool_request_user_comment": "Comment from user: {comment}",
        "tool_request_rejected_with_comment": ":x: Tool permission request cancelled (relaying your comment to Claude)",

        # ── prompt (Claude向け — 英語圏でも理解可能) ──
        "prompt_attached_files": "Attached files:",
        "prompt_answer_to_question": "Answer to the following question:\nQuestion: {question}",
        "prompt_answer_to_prev": "Answer to the previous question:",
        "prompt_options_label": "Options:",
        "prompt_answer_numbered": "Answer: {num}. {label}",
        "prompt_answer_text": "Answer: {label}",
        "prompt_system_append": (
            "When you need to ask the user a question or confirmation, "
            "do not use the AskUserQuestion tool. Instead, output your question as text. "
            "After outputting the question, end your response immediately. "
            "The user will answer in the next message. "
            "Do not proceed without waiting for the answer. "
            "When exiting plan mode, do not use the ExitPlanMode tool. "
            "Instead, output your plan as text. "
            "The user will approve the plan, and execution will begin in the next message. "
            "If a tool call is rejected due to insufficient permissions, "
            "or you need a tool not in your allowedTools, "
            "do NOT suggest manual execution or workarounds. "
            "Instead, ALWAYS output a marker at the end of your response: "
            "[TOOL_REQUEST:ToolName]. "
            "The user can approve it and the task will automatically retry with that tool enabled. "
            "Examples: [TOOL_REQUEST:Bash(npm install)] or [TOOL_REQUEST:WebFetch]. "
            "Output multiple markers if you need multiple tools."
        ),
        "prompt_allowed_tools_info": (
            "\nYour currently allowed tools: {tools}. "
            "If you need any tool not in this list (e.g. a Bash command not matching the allowed pattern, "
            "WebFetch for downloading, etc.), do NOT suggest manual execution. "
            "Instead, ALWAYS output a [TOOL_REQUEST:ToolName] marker."
        ),

        # ── App Home dashboard ──
        "home_title": ":satellite_antenna: Claude Session Dashboard",
        "home_subtitle": "View: {scope}  ·  updated {updated}",
        "home_scope_mine": "Mine",
        "home_scope_all": "Everyone",
        "home_toggle_to_all": ":busts_in_silhouette: Show everyone",
        "home_toggle_to_mine": ":bust_in_silhouette: Show mine only",
        "home_refresh_button": ":arrows_counterclockwise: Refresh",
        "home_section_running": ":gear: *Running (bridge) — {count}*",
        "home_section_external": ":computer: *Running (terminal / external) — {count}*",
        "home_section_finished": ":checkered_flag: *Finished — {count}*",
        "home_none": "_none_",
        "home_more": "_{count} more_",
        "home_open_thread": ":speech_balloon: Open thread",
        "home_truncated": "_…truncated to fit the display limit_",
        "home_overview": ":open_file_folder: {groups} dirs  ·  running {running}  ·  finished {finished}",
        "home_group_summary": "_(running {running} · finished {finished})_",
        "home_group_unknown": "(unknown location)",
        "home_ext_line": ":computer: external PID `{pid}`  :clock1: {etime}",
        "home_fork_button": "fork",
        "home_not_allowed": ":lock: You are not allowed to view this dashboard.",
        "home_dur_sec": "{n}s",
        "home_dur_min": "{n}m",
        "home_dur_hour": "{n}h{m}m",
        "home_fork_thread_header": ":fork_and_knife: Imported the external session (cwd: `{cwd}`). Reply in this thread to continue.",
        "home_fork_failed": ":x: Fork failed.",
        "home_fork_no_channel": (
            ":mag: No channel matching cwd `{cwd}` was found.\n"
            "Choose where to import it."
        ),
        "home_fork_create_channel": ":heavy_plus_sign: Create a channel",
        "home_fork_use_dm": ":speech_balloon: Open in this DM",
        "home_fork_creating": ":hourglass_flowing_sand: Creating a channel…",
        "home_fork_opening_dm": ":hourglass_flowing_sand: Opening in DM…",
    },
}


def t(key: str, **kwargs) -> str:
    """翻訳キーからメッセージを取得。kwargs で format 変数を埋める。
    言語は毎回 os.getenv で取得する（load_dotenv() 後でも正しく動作するため）。"""
    lang = os.getenv("SLACK_LANGUAGE", "ja")
    msg = MESSAGES.get(lang, MESSAGES["ja"]).get(key)
    if msg is None:
        # フォールバック: ja → キーそのまま
        msg = MESSAGES["ja"].get(key, key)
    return msg.format(**kwargs) if kwargs else msg
