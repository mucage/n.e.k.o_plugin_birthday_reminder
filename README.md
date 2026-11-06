# 岁岁念 / Birthday Reminder

> 岁岁常欢愉，年年皆胜意。

GitHub：<https://github.com/mucage/n.e.k.o_plugin_birthday_reminder>

给 N.E.K.O. 的猫娘插件：记住朋友生日，在每天猫娘第一次主动搭话时提醒你；支持多次提醒，直到你确认已祝福或让她今天别再提醒。

![plugin](https://img.shields.io/badge/N.E.K.O-plugin-ff7aa2) ![sdk](https://img.shields.io/badge/SDK-%3E%3D0.1.0%2C%3C0.3.0-7aa2ff)

## 功能

- **名单编辑**：姓名、生日、备注、标记（提前提醒送礼 / 特别关心），直接在插件面板增删改。
- **xlsx / csv 导入**：自动识别表头「姓名 生日 备注 标记」，支持文本日期和 Excel 日期单元格；同姓名同月日更新，否则新增。
- **每天第一次主动搭话**：当天有人生日时，先在 `00:05` 后把生日备忘作为**上下文**交给猫娘（不单独发消息），让她在第一次主动搭话时自然提起；到 `08:30` 还没确认时再补一条明确提醒。
- **多次提醒**：默认每 60 分钟重试，每天最多 3 次；可在面板调整。
- **确认方式**：
  - 对猫娘说「已经发过祝福了」「刚发消息了」「打过电话了」「今天别提醒了」等，猫娘调用 `birthday_ack` 工具登记；
  - 面板点「标记已祝福」或「今天别再提醒」；
  - 插件也会从最近对话里识别常见说法作为兜底。
- **提前送礼**：标记「提前提醒送礼」的朋友，在生日前 N 天（默认 7 天）提醒准备礼物。
- **特别关心**：提高提醒优先级、名单置顶，并让猫娘措辞更认真。
- **无第三方依赖**：xlsx 解析使用标准库 `zipfile` + XML，不要求 `openpyxl`；网络请求为零。

## 目录结构

```text
birthday_reminder/
├── plugin.toml            # 插件清单 / Hosted UI 声明
├── __init__.py            # 插件类：生命周期、定时器、入口、LLM 工具、UI context
├── birthday_core.py       # 纯逻辑：日期、调度、确认识别、xlsx/csv 解析（无 SDK 依赖）
├── ui/panel.tsx           # 插件面板（Hosted TSX）
├── i18n/                  # zh-CN / en
├── docs/quickstart.md     # 插件内「使用说明」页
├── examples/              # 示例表格
└── tests/                 # 单元测试 + SDK stub 冒烟测试
```

## 安装 / 开发模式加载

### 方式 A：开发模式（推荐先这样验证）

1. 打开安装版 N.E.K.O. 的插件管理器。
2. 点左侧「服务器日志」下方的「开发模式」，开启开关。
3. 点「加载未打包插件」，选择本目录（包含 `plugin.toml` 的那一层）的**绝对路径**。
4. 点「校验」→「加载未打包插件」→ 启动。
5. 打开插件面板，添加或导入生日；打开「自动启动」。
6. 修改代码后，在开发插件卡片上点「重载」。

> 开发模式只对本机桌面端 / 回环网页开放。源码目录名必须是 `birthday_reminder`，与插件 ID 一致。

### 方式 B：打包成 `.neko-plugin`

在开发插件卡片上点「打包」下载安装包；或者如果有 N.E.K.O. 源码和 CLI：

```bash
uv run neko-plugin check birthday_reminder
uv run neko-plugin build birthday_reminder --out birthday_reminder.neko-plugin
```

然后在普通导入入口安装。打包前不要修改源码，检测到变化需要重试。

## 使用

1. 启动插件并打开「自动启动」。
2. 面板里添加朋友，或导入 `examples/生日模板.xlsx`。
3. 默认每天 `08:30` 开始提醒，`22:30` 停止；可在面板底部修改。
4. 对猫娘说已经祝福，或点面板按钮确认。
5. 想先预览猫娘会怎么说，点「测试提醒」：立即发一条，不消耗当天次数，也不改变确认状态。

更完整的说明见插件内「使用说明」页，或 [docs/quickstart.md](docs/quickstart.md)。

## 表格格式

| 姓名 | 生日 | 备注 | 标记 |
| --- | --- | --- | --- |
| 小林 | 1995-08-12 | 喜欢咖啡 | 提前提醒送礼 |
| 阿澄 | 08-12 | 大学同学 | 特别关心 |
| 小夏 | 2000/2/29 |  |  |

- 表头别名：姓名/名字/name，生日/出生日期/birthday，备注/说明/note，标记/标签/tags。
- 列顺序可以换；表头缺失时按 A=姓名、B=生日、C=备注、D=标记 读取。
- 标记多个用逗号、顿号或空格分隔。
- `.xls` 不支持，请另存为 `.xlsx`。

## 提醒机制与设计说明

插件 API 不能修改宿主已经生成好的主动消息，所以采用两层：

1. **上下文 cue**（`ai_behavior="read"`）：当天第一次 tick 时把生日备忘送进猫娘上下文，不触发对话。如果宿主随后发起当天第一次主动搭话，猫娘可以看到并自然提起。
2. **明确提醒**（`ai_behavior="respond"`）：到 `remind_after` 仍未确认时，插件主动触发一次 AI 回合，让猫娘开口。之后按间隔重复，直到确认或当天停止。

这意味着：

- 如果宿主在 cue 之前就已经主动搭话，那次搭话不会包含生日；插件会在设定时间补一条提醒。
- 如果宿主关闭了主动搭话，插件仍会通过 `respond` 让猫娘开口。
- 已生成的消息无法被插件编辑；这是宿主 API 的边界，不是插件刻意多发。

「特别关心」目前只影响优先级、排序和措辞；「提前提醒送礼」在生日前 N 天触发一次准备礼物的提醒。

## 数据

- 数据文件：`<用户数据目录>/plugins/birthday_reminder/data/birthdays.json`（Windows 默认 `%LOCALAPPDATA%\N.E.K.O\plugins\birthday_reminder\data\`）。
- 原子写入（临时文件 + `os.replace`），损坏时自动备份为 `birthdays.json.corrupt` 并重新开始。
- 只读取最近对话轮次做兜底确认，不保存对话原文；确认备注最长 80 字。
- 不发起任何网络请求。

## 开发与测试

纯逻辑测试（26 个用例）：

```bash
python -m unittest discover -s tests -v
```

插件类冒烟测试（10 个用例，用 `tests/stub` 替代 SDK）：

```powershell
$env:PYTHONPATH="$PWD;$PWD\tests\stub"
python tests\test_plugin.py -v
```

TypeScript 面板类型检查（在装有 `typescript` 的环境）：

```bash
tsc --noEmit -p tsconfig.json
```

## 市场发布

仓库已按官方插件市场规范补齐：

- `.github/workflows/verify.yml` — 每次 push / PR 调用官方 `plugin-market-verify.yml`；
- `.github/workflows/release.yml` — 推送 `v*` tag 时调用官方 `plugin-market-release.yml`；
- `ruff.toml`、`.vscode/`、`tests/test_smoke.py`、`.gitignore` 等仓库检查所需文件。

发布时只需推送与 `plugin.toml` 版本一致的 tag，例如 `v1.0.0`。Market 投稿与审核仍需在 N.E.K.O. 市场页面完成。

## 兼容性

- Python 3.11+（宿主运行时为 3.11）。
- SDK `>=0.1.0,<0.3.0`；使用 `NekoPluginBase`、`@plugin_entry`、`@lifecycle`、`@timer_interval`、`@llm_tool`、`@message`、`@ui.context`、`@ui.action`、`push_message` v2。
- Hosted UI 运行时要求；不支持 Static UI 旧路径。

## 已知限制

- 不能改写宿主已经生成的主动消息，只能“先放上下文 + 到点补提醒”。
- `.xls` / 加密 xlsx / 多工作表只读第一个工作表；需要多表时请先合并。
- 目前按公历计算，不支持农历生日。
- 2 月 29 日在非闰年按 2 月 28 日提醒。
- 多角色场景下，定时提醒会尽量发给当前角色；若宿主没有暴露当前角色，则按宿主默认广播行为处理。
- 表头必须是中文或英文常见别名；完全自定义的表头会走 A-D 回退并在导入结果里提示。

## License

与 N.E.K.O. 插件生态一致，按 Apache-2.0 使用。
