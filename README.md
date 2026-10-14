# yf-skill-check

**给你的 skills 做一次体检：检查触发重叠，估算上下文开销，生成一份中文交互网页报告。**

装了两个修 bug 的 skill，或者几个设计、封面 skill，遇到同一个请求时该选谁？`yf-skill-check` 先从简介筛出候选，再由当前智能体阅读相关说明，判断它们是冲突、分工，还是适用于不同场景。报告帮你决定是否调整，以及先调整什么。

只检查和给建议，不修改、禁用或卸载已安装的 skills。

## 可以检查什么

| 检查项 | 报告会说明 |
| --- | --- |
| 触发冲突与职责重叠 | 共同触发的请求、适用条件、原文依据和分工建议 |
| 潜在 Token 开销 | 技能目录的估算占用，以及指定场景中额外读取说明的成本 |
| 重复副本与同名来源 | 是否指向同一文件、是否为独立副本、可能的来源优先级 |
| 简介范围与说明长度 | 作为配置信息记录，结合实际用途判断是否需要调整 |
| 本地链接和读取限制 | 说明中链接的文件是否存在、哪些文件未能读取或解析 |

> [!NOTE]
> 语义相似、说明较长、缺少 Markdown 链接，都不能单独证明一个 skill 有问题。合理的综合工作流、阶段协作和不同风格可以保持原样。

## 安装

需要 **Python 3.10 或更高版本**，以及能够读取本地文件、执行脚本的智能体。基础功能使用 Python 标准库，不需要额外配置模型 API Key；正文复核由当前会话模型完成，仍会使用该会话的额度。

先下载或克隆仓库：

```bash
git clone https://github.com/sunyifeng11111/yf-skill-check.git
cd yf-skill-check
```

将仓库中的 **`yf-skill-check/` 子文件夹**安装到平台的技能目录，保留其中的脚本、引用和 HTML 模板。不要将整个仓库当作一个 skill 安装。

macOS / Linux 可以使用软链接，让后续代码更新同步生效。选择你使用的平台执行一组命令：

```bash
# Codex
mkdir -p ~/.agents/skills
ln -s "$PWD/yf-skill-check" ~/.agents/skills/yf-skill-check

# Claude Code
mkdir -p ~/.claude/skills
ln -s "$PWD/yf-skill-check" ~/.claude/skills/yf-skill-check

# Cursor
mkdir -p ~/.cursor/skills
ln -s "$PWD/yf-skill-check" ~/.cursor/skills/yf-skill-check

# WorkBuddy：使用本机已配置的技能目录
mkdir -p ~/.workbuddy/skills
ln -s "$PWD/yf-skill-check" ~/.workbuddy/skills/yf-skill-check
```

目标位置已有同名技能时，先确认现有安装；上述命令不会覆盖它。也可以直接复制子文件夹。安装后按平台需要刷新技能列表或开启新会话。

目录与调用方式可参考 [Codex 文档](https://learn.chatgpt.com/docs/build-skills)、[Claude Code 文档](https://code.claude.com/docs/en/skills)和 [Cursor 文档](https://cursor.com/docs/skills)。

## 开始体检

在智能体中输入：

```text
使用 yf-skill-check，检查当前平台已安装的 skills，生成中文 HTML 体检报告。
只给建议，不修改任何已安装技能。
```

Codex 可以用 `$yf-skill-check` 显式调用；Claude Code 可以用 `/yf-skill-check`。其他平台按其技能调用方式使用。

技能会识别当前宿主，扫描它的技能来源，复核候选，然后在工作目录下的 `skill-health-report/` 中生成报告。已有报告时会使用新的时间戳目录。

打开生成目录中的 **`report.html`**，无需部署。报告自身不发送网络请求，可以离线查看；扫描文件和模型复核仍在当前智能体环境中进行。

> [!IMPORTANT]
> `yf-skill-check/assets/report.html` 是模板，没有体检数据。请打开智能体生成的报告，而不是这个模板文件。

## 报告怎么看

- **简报**：一句结论、最多三条优先建议和 Token 概览。先看是否需要调整。
- **可以留意**：尚未确认实际影响的事项，默认收起，可以先保持配置。
- **Token 模拟**：选择已检查的请求示例，或手动选择主技能和可能额外读取的技能。
- **完整明细**：技能组合、文件记录、技能列表和检查说明。支持筛选、搜索、分页和展开原文。“文件记录”指配置检查结果，不是修改历史，也不代表每条都有问题。

报告可导出 JSON，也可通过浏览器打印或保存为 PDF。未完成复核、候选超出数量上限或文件无法读取时，会明确说明检查限制。

## 范围与估算边界

本项目默认实现 Codex、Claude Code、Cursor 的常见本地目录发现规则，**一次体检只检查当前宿主的来源**。插件、内置、企业或同步来源需要宿主补充真实路径或当前会话技能列表。

WorkBuddy 等其他宿主使用 `other` 模式，由智能体提供实际技能目录，报告会标记平台适配未验证。安装目录本身不能证明当前宿主身份，也不能证明所有技能都已进入模型上下文。详细规则见 [平台发现说明](yf-skill-check/references/platforms.md)。

Token 默认按字符估算，并展示方法和范围；它不是实际账单，也不是统计置信区间。所有已安装技能正文的总和不等于每轮浪费。情景模拟只计算额外读取的 `SKILL.md` 正文，不包含引用文件、工具输出、聊天内容或缓存计费。

初筛与模型复核都可能漏检。已确认冲突表示说明中存在无法同时满足的要求，不表示已经观察到运行失败；本项目不读取历史对话、不追踪真实触发或费用。

## 手动运行与开发

通常直接让智能体调用 skill 即可。若要调试扫描器，可在仓库根目录运行：

```bash
# 静态初检：仅扫描明确指定的技能目录
python3 yf-skill-check/scripts/audit.py scan \
  --platform other \
  --only-roots \
  --root /path/to/skills \
  --host-evidence "当前宿主明确提供的技能目录" \
  --out skill-health-report/manual-scan
```

静态扫描会输出 `scan.json`、`summary.json`、`routing-index.json`、`review-tasks.json`、`finding-review-tasks.json` 和初检 HTML。**仅执行扫描不会完成语义复核**；让当前模型按 [复核协议](yf-skill-check/references/review.md) 写出 `reviews.json`，再生成最终报告：

```bash
python3 yf-skill-check/scripts/audit.py render \
  --scan skill-health-report/manual-scan/scan.json \
  --reviews skill-health-report/manual-scan/reviews.json \
  --out skill-health-report/manual-scan/report.html
```

每次扫描使用新的空目录。常用选项包括：`--catalog` 提供当前会话实际可见的技能列表，`--max-candidates` 调整候选数量上限，`--encoding` 使用环境中预先安装的 `tiktoken` 对指定编码计数。运行 `scan --help` 查看全部参数。

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖软链接去重与环路、独立副本、平台范围、调用限制、引用检查、证据真实性、文件变更、报告分级和 HTML 注入防护。其中页面启动检查需要 Node.js；未安装时该项会跳过。

```text
yf-skill-check/
├── SKILL.md                 技能入口与执行说明
├── agents/openai.yaml       Codex 显示与调用信息
├── scripts/audit.py         扫描、校验和报告生成
├── references/              平台发现规则与复核协议
└── assets/report.html       离线交互报告模板
tests/                      自动化测试
docs/                       产品设计说明
```

生成的报告可能包含个人目录、技能名称和原文片段，默认不纳入版本控制。分享前请自行检查报告内容。
