# research-bridge-kit 3.1.0

Claude Code × Codex 多代理研究系统（protocol V3.0）的可复用工具包。

在任何一个本地项目里部署以后，你只需要在对话里说“开始研究”：

- **Claude** 负责设计实验、审查结果；
- **Codex** 负责写代码、跑实验、提交 Git；
- 两边通过项目里的 `.bridge/` 文件交接，一方交付后另一方在几秒内接手；
- 没有工作时不消耗 token；
- 你随时可以在对话里暂停、提意见或结束。

架构说明见 `docs/ARCHITECTURE.md`（即你的 V3.0 规范，第六节已更新为事件驱动 watcher）。

## 工具包内容

```
research-bridge-kit/
├── README.md                 本文件
├── deploy.py                 部署 / 升级 / 自检脚本（Python 3.8+，只用标准库）
├── install_skill.cmd         把 skill 装进本机 Claude Code（双击）
├── skill/research-bridge/    skill 本体（SKILL.md）
├── configs/                  每个项目的部署配置（JSON）
├── docs/ARCHITECTURE.md      系统架构规范
└── template/                 部署到项目里的全部文件
    ├── .bridge/              协议、两个 helper、测试、watcher、prompt、子代理规范、R0000 模板
    ├── .claude/agents/       4 个只读研究子代理（泛化、统计、泄露审计、批判）
    ├── GIT_PROVENANCE.md     Git 溯源规范（项目里已有同名文件时不覆盖）
    └── HANDOFF.md            交接文档模板（项目里已有时不覆盖）
```

## 新项目怎么用（推荐：全程对话）

1. 把工具包解压到 `C:\Users\<你>\Documents\research-bridge-kit`。
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
python deploy.py --config configs\myproject.json --dry-run   # 只显示计划，不写任何文件
python deploy.py --config configs\myproject.json             # 部署
python deploy.py --verify --project D:\path\to\project       # 自检
```

## 部署之后（每个项目都一样）

| 做什么 | 怎么做 |
|---|---|
| 开 watcher（一直开着） | 双击 `.bridge\watcher\start_watcher.cmd` |
| 跟系统对话 | 桌面版 Code → Local → 项目文件夹，第一句发：“你是本项目的 Operator，先完整阅读 `.bridge/watcher/OPERATOR_PROMPT.md` 并按它工作，然后告诉我当前状态” |
| 开始 / 暂停 / 继续 / 结束 / 提意见 / 看进度 | 在 Operator 对话里直接说 |
| 看 agent 实时输出 | 双击 `.bridge\watcher\live_view.cmd`，或在 Operator 对话里说“打开实时视图”。浏览器里逐条显示两个 agent 的发言、推理、命令和输出 |
| CLI 没装或没登录 | 双击 `.bridge\watcher\install_clis.cmd` |
| 自检 | 让 Operator 跑 `bridgectl check` |

## 版本

- 3.1.0：加入只读实时视图（`live_view.cmd` / `bridgectl view`）和 `bridgectl check`。
- 3.0.0：第一个通用版本，基于 Assignment2 上运行的 V3.0 系统。

## 升级已有部署（比如 Assignment2）

```powershell
python deploy.py --upgrade-code --project "D:\path\to\project"
```

- 只替换代码文件：两个 helper、测试、watcher、bridgectl 和实时视图脚本。
- 被替换的旧文件会先备份到 `.bridge\deployment_backups\`。
- 不会碰文档、prompt、配置、约束和状态文件。
- 有任务正在运行时会拒绝执行。
- 升级完以后重开 watcher 窗口。

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
