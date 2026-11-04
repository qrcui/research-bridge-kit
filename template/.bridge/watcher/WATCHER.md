# Event-driven dispatch (protocol V3.0) — quick reference

English | [中文](WATCHER.zh-CN.md)

- **Run the watcher:** double-click `.bridge\watcher\start_watcher.cmd` and keep the window open. It checks state with Python only when files change (no tokens) and launches `claude -p` / `codex exec` only when there is work.
- **Talk to the system:** Claude desktop app → Code → Local → this project folder; first message: "You are the Operator of this project. Read `.bridge/watcher/OPERATOR_PROMPT.md` completely, follow it, then tell me the current state." Or double-click `operator.cmd` (command-line chat).
- **Watch the agents live:** double-click `.bridge\watcher\live_view.cmd` (or ask the Operator for `bridgectl view`). The browser shows every message, reasoning step, command and output of Claude and Codex, including Codex subagents. The viewer (`live_view.py`) runs in the background and the watcher restarts it if it stops; the page reconnects and resyncs by itself, so it never needs restarting. Read-only; closing the page does not affect the agents. Turn it off with `"live_view": {"enabled": false}` in `watcher_config.json`; its own log is `logs\live_view.log`.
- **Command line (optional):** `.bridge\watcher\bridgectl status | check | view | start | pause | pause-after | resume | end | request "input"`
- **No idle loops:** an agent that exits normally while its work is unchanged is not relaunched (stalled, the window alerts you); a non-zero exit is retried once after 120 s, then it stops. A file change or a watcher restart re-arms it.
- **Closing the watcher does not stop a running agent;** reopening adopts it. Emergency stop: `Stop-Process -Id <pid>` with the pid from `status`.
- **Configuration:** `watcher_config.json` (Python path, both CLIs' commands, arguments and models). Restart the watcher after editing.
- **Logs:** `watcher.log`; every agent run's input and output are in `logs\`.
- **CLIs missing or not logged in:** double-click `install_clis.cmd`.
