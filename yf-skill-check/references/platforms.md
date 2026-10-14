# 当前平台的发现范围

平台身份来自当前会话，而非安装位置。多个平台共享 `.agents/skills`，Cursor 也可能加载 `.claude/skills` 和 `.codex/skills`。默认脚本只实现下列本地可检查来源；宿主实际目录优先，插件、企业、同步与云端来源可通过 `--catalog` 或 `--root` 补充。目录规则会变化，出现不一致时核对当前平台的官方文档，并在报告中说明。

| 平台 | 默认扫描 | 同名与调用注意事项 |
| --- | --- | --- |
| Codex | 工作目录到仓库根的 `.agents/skills`，用户 `~/.agents/skills`，管理员 `/etc/codex/skills`；存在时兼容检查会话常见的 `~/.codex/skills` | 同名技能不自动合并；`agents/openai.yaml` 的 `policy.allow_implicit_invocation: false` 限制隐式调用。旧目录仅作发现线索，不保证会话可见。 |
| Claude Code | 工作目录到仓库根的 `.claude/skills` 和 `.claude/commands`，用户对应目录 | 用户来源覆盖项目同名来源；企业覆盖规则需宿主提供。插件带命名空间，用实际目录中的 name。`disable-model-invocation: true` 为显式调用。 |
| Cursor | 用户与项目 `.cursor/skills`、`.agents/skills`、兼容 `.claude/skills` 与 `.codex/skills` | 不假设未经核实的同名优先级；嵌套目录可能只在相关文件上下文中可见。 |
| other | 不猜测目录，必须由当前宿主提供 `--root` 或实际 `--catalog` | 发现和优先级均标注未验证。 |

有 Git 仓库时向上检查到仓库根；无仓库时保守地只检查工作目录及用户根。访问嵌套项目可能加载的额外目录需执行模型补充，不默认递归整个工作区。脚本跟踪技能目录中的符号链接，用真实 SKILL.md 路径去重，记录全部入口。独立副本内容相同仍作为不同安装记录，标记重复副本，不断言平台必然重复注入。

`--catalog` 声明条目为当前会话可见；目录扫描发现但不在 catalog 中的条目标记未验证。它不证明宿主目录的精确字符串格式、裁剪规则或实际计费。显式调用技能可能仍出现在用户选择器，但不视为普通请求的隐式冲突候选。

来源：[Codex Build skills](https://learn.chatgpt.com/docs/build-skills)、[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Cursor Agent Skills](https://cursor.com/docs/skills)。
