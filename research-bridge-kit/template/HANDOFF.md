# {{PROJECT_NAME}} 交接文档

> 创建于 {{DEPLOYED_AT}}（research-bridge 部署模板）　｜　截止：{{DEADLINE}}
>
> 读这一份就能接手：任务是什么、现在交付到哪一步、做过哪些实验（有效 / 无效）、怎么复现、还剩什么没做。
> 本文件仍是部署模板时，Claude Research Lead 会在设计 R0001 前根据项目实际文件把第 0–6 节填好。

---

## 协作约定（多个 agent 共用本文件沟通）

- **只追加，不覆盖**：不要删除或改写别人写的内容。发现记录有误时，在后面追加更正，注明谁、在什么日期更正了什么。
- **每段内容都署名、注明日期**：标题写成 `## [Codex] 2026-01-01 标题` 这种格式；新增章节放到文件末尾。
- **第 0–8 节由 Claude Research Lead 维护**。其他 agent 需要更新其中的状态时，在文件末尾追加说明。
- 本文件是研究记录，不是任务信号。正式任务只通过 `.bridge/tasks/` 和 `.bridge/signals/` 发布。
- 改动受保护文件（见 `.bridge/PROJECT_CONSTRAINTS.md`）之前，必须有用户的明确授权，并在这里写明原因和备份位置。

---

## 0. 一句话现状

（待填写）

## 1. 任务与约束（规则全文见 `.bridge/PROJECT_CONSTRAINTS.md`）

{{RESEARCH_GOAL}}

## 2. 当前交付物

（待填写：文件、版本、指标）

## 3. 当前方案

（待填写）

## 4. 实验记录

### 4.1 有效的

### 4.2 无效或变差的（不要重复试）

## 5. 可以继续尝试的方向

## 6. 复现与操作指南

- Python：`{{PYTHON}}`
- 多代理系统：见 `.bridge/watcher/WATCHER.md`

## 7. 未完成事项 / 待用户决定

## 8. 时间线

- {{DEPLOYED_AT}}：部署 Claude Code × Codex 研究系统（protocol V3.0），生命周期 IDLE，未开始研究。
