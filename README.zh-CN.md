# research-bridge-kit 3.2.0

[English](README.md) | 中文

Claude Code × Codex 多代理研究系统（protocol V3.0）的可复用工具包。

在任何一个本地项目里部署以后，你只需要在对话里说“开始研究”：

- **Claude** 负责设计实验、审查结果；
- **Codex** 负责写代码、跑实验、提交 Git；
- 两边通过项目里的 `.bridge/` 文件交接，一方交付后另一方在几秒内接手；
- 没有工作时不消耗 token；
- 你随时可以在对话里暂停、提意见或结束。

架构说明见 [`docs/ARCHITECTURE.zh-CN.md`](docs/ARCHITECTURE.zh-CN.md)。

## 工具包内容

```
research-bridge-kit/
├── README.md / README.zh-CN.md     说明（英文 / 中文）
├── deploy.py                       部署 / 升级 / 自检脚本（Python 3.8+，只用标准库）
├── install_skill.cmd               把 skill 装进本机 Claude Code（双击）
├── skill/research-bridge/          skill 本体（SKILL.md）
├── configs/                        每个项目的部署配置（JSON，不上传 Git）
├── docs/                           系统架构规范（英文 / 中文）
└── template/                       部署到项目里的全部文件
    ├── .bridge/                    协议、两个 helper、测试、watcher、prompt、实时视图、子代理规范、R0000 模板
    ├── .claude/agents/             4 个只读研究子代理（泛化、统计、泄露审计、批判）
    ├── GIT_PROVENANCE.md           Git 溯源规范（项目里已有同名文件时不覆盖）
    └── HANDOFF.md                  交接文档模板（项目里已有时不覆盖）
```

给 agent 读的文件（协议、prompt、子代理规范、skill）都是英文；给人读的文档（README、架构、使用速查）是中英双语。

## 新项目怎么用（推荐：全程对话）

1. 在 GitHub 页面点 **Code → Download ZIP**，解压后把文件夹改名为 `research-bridge-kit`，放到 `C:\Users\<你>\Documents\` 下。
2. 双击 `install_skill.cmd`，把 skill 装进本机 Claude Code。这一步只需做一次。
3. 打开 Claude 桌面版，进入 **Code** 页签，选 **Local**，文件夹选你的新项目，然后说：**“给这个项目部署多代理研究系统”**。
4. 它会先只读地检查项目，然后一次性问你几个问题：研究目标、怎么评价、哪些文件不能动、数据规则、截止时间、轮次预算、用哪个 Python。
5. 回答完以后，它会依次完成：
   - 部署；
   - 和你一起确认 Git 基线；
   - 检查两个 CLI 有没有装好、有没有登录；
   - 运行自检；
   - 教你后续怎么用。
6. 部署完成时系统处于 **IDLE**，不会自己开始。准备好以后，在 Operator 对话里说“开始研究”。

## 不用对话也行（手动）

```powershell
python deploy.py --example-config --out configs\myproject.json   # 生成配置模板，填好各项
python deploy.py --config configs\myproject.json --dry-run       # 只显示计划，不写任何文件
python deploy.py --config configs\myproject.json                 # 部署
python deploy.py --verify --project D:\path\to\project           # 自检
```

## 部署之后（每个项目都一样）

| 做什么 | 怎么做 |
|---|---|
| 开 watcher（一直开着） | 双击 `.bridge\watcher\start_watcher.cmd` |
| 跟系统对话 | 桌面版 Code → Local → 项目文件夹，第一句发：“你是本项目的 Operator，先完整阅读 `.bridge/watcher/OPERATOR_PROMPT.md` 并按它工作，然后告诉我当前状态” |
| 开始 / 暂停 / 继续 / 结束 / 提意见 / 看进度 | 在 Operator 对话里直接说 |
| 看 agent 实时输出 | 双击 `.bridge\watcher\live_view.cmd`，或在 Operator 对话里说“打开实时视图”。浏览器里逐条显示两个 agent（包括 Codex 子代理）的发言、推理、命令和输出。详见[实时视图](#实时视图) |
| CLI 没装或没登录 | 双击 `.bridge\watcher\install_clis.cmd` |
| 自检 | 让 Operator 跑 `bridgectl check` |

## 实时视图

一个只读网页，地址是 `http://127.0.0.1:8765/`（端口被占用时依次用下一个），实时显示两个 agent 正在做什么。

- **一直在线**：查看服务（`.bridge/watcher/live_view.py`，只用 Python 标准库）在后台运行。watcher 负责启动它，每 30 秒检查一次，停了就重新拉起。`live_view.cmd` / `bridgectl view` 只负责打开网页（watcher 没开时也会顺便启动查看服务）。
- **不用重启**：页面断线会自己重连，查看服务重启后页面自动重新同步。新的 Claude 运行、新的 Codex 会话和 Codex 子代理都会自动出现，不用刷新；子代理结束后 Codex 主会话还在干活，也会继续跟着显示。
- **长时间运行也不卡**：查看服务只保留最近约 5000 条事件，页面只保留最近约 2000 行；很长的命令输出要展开那一行时才加载。完整内容始终在 agent 日志里。
- **安全**：只监听 127.0.0.1，只读不写 agent 文件，不碰任何 bridge 状态。
- **设置**：`.bridge\watcher\watcher_config.json` 里的 `"live_view": {"enabled": true, "port": 8765}`（改完重启 watcher）。它自己的日志是 `.bridge\watcher\logs\live_view.log`。
- **其他命令**：`<项目 python> .bridge/watcher/live_view.py status | stop | open`。查看服务本身在 Linux 和 macOS 上也能运行（`python3 .bridge/watcher/live_view.py open`）。

## 升级已有部署

```powershell
python deploy.py --upgrade-code --project "D:\path\to\project"
```

- 只替换代码文件：两个 helper、测试、watcher、bridgectl 和实时视图相关文件。
- 被替换的旧文件会先备份到 `.bridge\deployment_backups\`。
- 不会碰文档、prompt、配置、约束和状态文件。
- 有任务正在运行时会拒绝执行。
- 升级完以后重开 watcher 窗口。从 3.1 升级时，如果旧的实时视图窗口还开着，先关掉它；重开的 watcher 会自动启动新的后台查看服务。

## 系统要求

- Windows 10/11，PowerShell 5.1 或更高版本。
- 项目用的 Python 必须是 **3.12 或更高版本**（两个 helper 需要）；`deploy.py` 本身只需要 3.8 以上。
- Claude Code CLI 和 Codex CLI 都已安装并登录。
- Git：可选，但推荐安装。

## 安全默认

- **部署不等于开始**：只有你明确说“开始研究”才会运行。重启电脑、打开 watcher、旧的任务信号都不会让它自己运行。
- **不覆盖已有文件**：项目里已有的同名文件不会被覆盖。部署前会检查 `.bridge/`，已存在就拒绝部署。
- **受保护文件**：写进 `PROJECT_CONSTRAINTS.md` 的受保护文件，没有你的授权，任何 agent 都不能改。
- **跳过权限确认**：两个 agent 无人值守运行，默认跳过权限确认，否则会卡在审批弹窗上。约束它们的是 `PROTOCOL.md` 和 `PROJECT_CONSTRAINTS.md`。想收紧的话，修改 `.bridge\watcher\watcher_config.json` 里的参数。
- **不外传**：不打包、不上传、不推送、不建公开仓库，除非你明确要求。

## 版本

- 3.2.0：实时视图用 Python 重写：由 watcher 保持运行，页面自动重连并重新同步，跟踪所有正在运行的 Codex 会话（包括子代理），内存有上限，长输出按需加载，只接受本机地址的请求；`bridgectl check` 和 `deploy.py --verify` 也会跑它的测试。
- 3.1.0：加入只读实时视图（`live_view.cmd` / `bridgectl view`）和 `bridgectl check`；文档改为中英双语。
- 3.0.0：第一个通用版本，来自一个实际运行中的 V3.0 部署。

## 许可证

MIT，见 [LICENSE](LICENSE)。
