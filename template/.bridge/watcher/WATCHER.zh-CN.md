# 事件驱动调度（protocol V3.0）速查

[English](WATCHER.md) | 中文

- **开 watcher**：双击 `.bridge\watcher\start_watcher.cmd`，窗口一直开着。它只在文件变化时用 Python 检查状态（不花 token），有活才拉起 `claude -p` / `codex exec`。
- **跟系统对话**：Claude 桌面版 → Code → Local → 选本项目文件夹，第一句发“你是本项目的 Operator，先完整阅读 `.bridge/watcher/OPERATOR_PROMPT.md` 并按它工作，然后告诉我当前状态”。或双击 `operator.cmd`（命令行版对话）。
- **看 agent 实时输出**：双击 `.bridge\watcher\live_view.cmd`（或让 Operator 执行 `bridgectl view`），浏览器里逐条显示 Claude 和 Codex（包括 Codex 子代理）的发言、推理、命令和输出。查看服务（`live_view.py`）在后台运行，停了 watcher 会自动重新拉起；页面断线会自己重连并重新同步，不需要重启。只读，关掉页面不影响 agent。想关闭它，在 `watcher_config.json` 里设 `"live_view": {"enabled": false}`；它自己的日志是 `logs\live_view.log`。
- **命令行（可选）**：`.bridge\watcher\bridgectl status | check | view | start | pause | pause-after | resume | end | request "意见"`
- **防空转**：agent 正常退出但状态没变 → 不重复拉起（stalled，窗口报警）；非零退出 → 120 秒后重试一次，再失败就停。文件变化或重启 watcher 解除。
- **关 watcher 不会停掉正在跑的 agent**；重开会接管。紧急停止用 `status` 里的 pid：`Stop-Process -Id <pid>`。
- **配置**：`watcher_config.json`（python 路径、两个 CLI 的命令和参数、模型）。改完重启 watcher。
- **日志**：`watcher.log`；每次 agent 运行的输入输出在 `logs\`。
- **CLI 没装 / 没登录**：双击 `install_clis.cmd`。
