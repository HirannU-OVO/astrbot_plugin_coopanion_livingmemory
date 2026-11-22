# LivingMemory 桥接（astrbot_plugin_coopanion_livingmemory）

让 QQ 上的 AstrBot 和桌面上的桌宠共用同一份长期记忆。

> ⚠️ **配套说明**
> - 本插件运行在 **AstrBot** 侧，负责把桌宠发来的请求转接给上游记忆插件；
> - 桌宠侧必须成对安装配套扩展：[cortico-world-astrbot-livingmemory](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory)；
> - 两个插件通过本地 HTTP 接口交互，**必须成对安装运行**。

## 先看这里：你的记忆里有没有 AstrBot

第一步，在 AstrBot 的记忆桥设置里选「使用场景」：

- 只有桌宠、没有 AstrBot 参与 → 选「纯桌宠」
- AstrBot 也参与对话（它自己回 QQ，或桌宠接管了它的对话）→ 选「接入 AstrBot」

第二步，如果上一步选了「接入 AstrBot」，再到桌宠设置里选「桌宠的记忆」：

- 「与 AstrBot 共享」→ 桌宠和 AstrBot 用同一份记忆，能互相召回
- 「桌宠单独一份」→ 桌宠自己记，AstrBot 看不到

选了「纯桌宠」的话，第二步不用管，那个下拉不起作用。

## 架构与分工

```
Coopanion 桌宠
  │  桌宠侧扩展：记忆检索 / 记录 + 对话流推送
  ▼
cortico-world-astrbot-livingmemory（桌宠侧扩展）
  │  本地 HTTP（默认 127.0.0.1:6198）
  ▼
astrbot_plugin_coopanion_livingmemory（本插件，AstrBot 侧）
  │  转接给 AstrBot 的记忆插件
  ▼
astrbot_plugin_livingmemory（记忆库 + 知识图谱 + 会话蒸馏）
```

本插件不单独存数据、不做向量化；记忆的保存、检索和图谱都由上游 `astrbot_plugin_livingmemory` 负责。

## 相关项目

| 项目 | 作者 | 位置 | 作用 |
| :--- | :--- | :--- | :--- |
| [Cortico 桥](https://github.com/as124da1231/astrbot_plugin_cortico_bridge)（`astrbot_plugin_cortico_bridge`） | [@as124da1231](https://github.com/as124da1231) | AstrBot 插件 | 管「对话」：让 Cortico/桌宠人格直接在 QQ 里说话，AstrBot 只当手脚和账号 |
| [cortico-world-astrbot](https://github.com/as124da1231/cortico-world-astrbot) | [@as124da1231](https://github.com/as124da1231) | 桌宠扩展 | 和上一行成对，把桌宠人格接到 QQ |
| [本插件](https://github.com/HirannU-OVO/astrbot_plugin_coopanion_livingmemory) + [桌宠扩展](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory) | [@HirannU-OVO](https://github.com/HirannU-OVO) | 本仓库 + 桌宠扩展 | 管「记忆」：把桌宠和 QQ 的记忆并到同一套 LivingMemory |
| [cortico-world-qq-stickers](https://github.com/HirannU-OVO/cortico-world-qq-stickers) | [@HirannU-OVO](https://github.com/HirannU-OVO) | 桌宠扩展 | QQ 表情包库：按情绪检索、掷骰配图，收到的表情自动归档 |
| [cortico-world-qq-initiative](https://github.com/HirannU-OVO/cortico-world-qq-initiative) | [@HirannU-OVO](https://github.com/HirannU-OVO) | 桌宠扩展 | QQ 主动搭话：按配额、冷却和白名单让桌宠先开口 |

> 前两项是 [@as124da1231](https://github.com/as124da1231) 的作品，谢谢他把桌宠接进 QQ 这条路先走了出来。后面三个是 [@HirannU-OVO](https://github.com/HirannU-OVO) 维护的 QQ 系列，和它们功能不重叠，可以一起装，配着用正好。

上面两项是一对，负责「对话」；本插件这一对负责「记忆」。两者功能不重叠，**可以同时装**：装了 Cortico 桥，QQ 那头的回答由 Cortico 人格来出，记忆照样走本插件。

本插件不依赖 Cortico 桥和 cortico-world-astrbot；只有想让桌宠人格在 QQ 里说话时才需要装它们。

---

## 安装与快速上手

> **多端一起升级**：本插件、桌宠扩展 `cortico-world-astrbot-livingmemory`、QQ 消息接入扩展 `cortico-world-astrbot` 要一起升级；只升一侧会出现面板项对不上。若同时使用 Cortico 桥，还需要 `astrbot_plugin_cortico_bridge` 1.3.7 或更高。
>
> **Cortico 桥安装提示**：AstrBot 插件面板里默认的插件源指向旧版本，装不到 1.3.7；需要手动把插件源改成 GitHub 仓库 https://github.com/as124da1231/astrbot_plugin_cortico_bridge 再安装或更新。

### 1. 安装前置插件

先在 AstrBot 中安装并启用上游记忆插件：

- [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) —— 感谢 **lxfight** 的开源作品：记忆库、知识图谱和会话蒸馏都是它做的，本仓库只是调用它的公开接口。

装好后请按[上游 README](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)确认嵌入模型和向量索引配置正确。配置不对时，最典型的现象是桌宠什么都记不住。

### 2. 安装本插件

**面板安装（推荐）**：AstrBot 管理面板 → 插件 → 安装

**命令行安装（可选）**：

```bash
git clone https://github.com/HirannU-OVO/astrbot_plugin_coopanion_livingmemory.git data/plugins/astrbot_plugin_coopanion_livingmemory
```

安装后重启一次 AstrBot。

### 3. 安装桌宠侧扩展

安装配套的桌宠扩展：[cortico-world-astrbot-livingmemory](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory)。

**面板安装（推荐）**：Coopanion → World扩展 → 扩展市场 → 安装

**手动安装（可选）**：把它的目录放到桌宠的扩展目录：

```text
%COOPANION_DIR%/data/extensions/local/cortico-world-astrbot-livingmemory/
```

装好之后在 Coopanion 的扩展管理里启用它。

### 4. 首次配置

两边各填一遍，把它们对上：

1. 「AstrBot 的地址」默认就是 `http://127.0.0.1:6198`，没改过端口就不用动；
2. 如果 AstrBot 侧开了「校验 token」，把它填到桌宠的「校验 token」里，两边保持一致；
3. 桥接插件首次连接后，会自动填好这几项：桌宠的 QQ 号、你的称呼、记忆存到哪个会话、记忆存给哪个人格；确认无误保存即可，你手动改过的项之后不会再被自动识别结果覆盖；
4. 桥接插件那边保存后立即生效；桌宠这边的配置改完要重载扩展（或重开桌宠）才读取新值。

### 5. 装完怎么确认成功

1. 在桌宠里说一句「查一下记忆通不通」，它会调 `mem_status` 报出桥接状态：上游记忆库显示「已连接」、使用场景和你的实际用法一致，就说明通了；
2. 接了 QQ 的话，也可以在 QQ 里直接对机器人发 `/lmbridge`，桥接插件面板里能看到同一份状态；纯桌宠没有平台，这一步跳过；
3. 在桌宠里正常聊天，等上游完成一次记忆总结（默认约 10 轮对话触发）后，再去问它记不记得。只聊一句不会立刻写进记忆，这是正常的。

---

## 工具 / 接口列表

本插件默认监听 `http://127.0.0.1:6198`，供桌宠侧扩展调用：

| 接口 | 作用 |
| :--- | :--- |
| `GET /api/health` | 查看桥接服务与记忆插件是否正常 |
| `POST /api/memory/search` | 检索长期记忆（语义 + 关键词 + 知识图谱；可选 `queries` 多段融合） |
| `POST /api/memory/add` | 写入一条记忆 |
| `POST /api/memory/batch_add` | 批量写入记忆（迁移/导入用） |
| `POST /api/memory/chat` | 接收桌宠对话，交给记忆插件做后台总结 |

> **会话参数为必填**：调用 `/api/memory/search`、`/recall`、`/evidence`、`/add`、`/memorize`、`/batch_add`、`/forget`、`/reflect`、`/chat` 时必须显式传 `session_id`，格式 `<persona>:<MessageType>:<id>`（如 `MyBot:GroupMessage:123456789`、`MyBot:FriendMessage:10000`）。缺少 session 时读接口返回 `count: 0` + `code: missing_session`，写接口返回 400 `missing_session`，不再回落到默认会话。
>

| `POST /api/memory/reflect` | 手动触发一次会话总结 |
| `GET /api/tools/manifest` | 下发当前可用工具清单与版本，桌宠侧据此动态生成工具 |
| `POST /api/memory/memorize` | 深度结构化沉淀（提炼关键事实与情感倾向） |
| `POST /api/memory/recall` | 精准召回（可按说话人或时间范围） |
| `POST /api/memory/evidence` | 深读某条记忆的结构化事实与原文证据 |
| `POST /api/memory/forget` | 归档并软删除记忆（仅管理员，鉴权用 AstrBot 全局配置里的 `admins_id`） |

如果配置了「校验 token」，所有 `/api/*` 请求都需要带上 `Authorization: Bearer <你的 token>`。

### 多段检索（`queries`）

`/api/memory/search` 除了单个 `query`，还可以传 `queries`（字符串数组，最多 4 段）。桥端逐段检索、再用 RRF 把各路结果融合成一份，适合一次问几件事、或把长问题拆成几个关键词。

不传 `queries` 时行为与单段完全一致，原有调用方不受影响。**这一项需要桥端 2.3.0 及以上**：旧版会忽略这个字段、按空查询返回无关结果。

### 关键词子串增强（`recall_keyword_boost`）

默认开启。开启后除向量 / BM25 之外会多走一条「关键词子串」检索：从查询里抽出英文标识、中文短词、数字 ID，直接在上游记忆正文里做子串匹配，再按 RRF 并入结果。专有名词、工具名、型号、文件名这类稀有词靠它才能稳定召回。关掉后行为与没有这条检索时完全一致。面板里叫「关键词子串增强」。

### `mem_forget` 的管理员鉴权

软删除只对管理员开放，判定用的是 **AstrBot 全局配置里的 `admins_id`**，不是本插件的面板项：在 AstrBot 面板的配置页填，或直接编辑 `data/cmd_config.json` 里的 `admins_id`（字符串数组，写 QQ 号）。这一项为空时**一律不放行**，避免没配管理员就能归档记忆。

---

## 配置参数速查

| 面板显示名 | 配置 id | 类型 | 默认值 | 说明 |
| :--- | :--- | :---: | :---: | :--- |
| 监听端口 | `port` | 整数 | `6198` | 桌宠桥的监听端口，两边必须一致 |
| 监听地址 | `host` | 字符串 | `127.0.0.1` | 默认只允许本机；外部机器连过来要改成 `0.0.0.0`，并设置校验 token |
| 校验 token | `auth_token` | 字符串 | 无 | 和桌宠扩展填同一串；留空不校验 |
| 使用场景 | `usage_mode` | 单选 | `接入 AstrBot` (`qq`) | 接入 AstrBot 才有平台账号；纯桌宠只有本机 |
| 记忆共享模式 | `memory_share_mode` | 单选 | `一起记` (`shared`) | 一起记＝两边共用一套；桌宠单独记＝各记各的 |
| 桌宠的 QQ 号 | `bot_qq` | 字符串 | 自动检测 | 机器人的 QQ 号，留空由首次连接自动识别 |
| 桌宠的称呼 | `bot_name` | 字符串 | 自动检测 | 机器人的名字，留空由首次连接自动识别 |
| 桌宠的其他叫法 | `bot_aliases_text` | 列表 | 无 | 机器人的别名，一行一个，按需添加 |
| 你的称呼 | `user_name` | 字符串 | 自动检测 | 桌宠这边怎么称呼你，纯桌宠场景下生效 |
| 谁是谁 | `user_map` | 列表 | 无 | 好友身份映射，一行一个（`QQ号=名字,别名`） |
| 自动总结桌宠对话 | `enable_auto_reflection` | 布尔 | `true` | 定期自动将桌宠日常对话提炼为长期记忆 |
| 自动建立记忆关联 | `enable_auto_graph_metadata` | 布尔 | `true` | 自动补全长期记忆与实体之间的知识图谱关联 |
| 记忆存给哪个人格 | `default_persona_id` | 字符串 | 自动检测 | 留空自动识别当前活跃人格，一般不用填 |
| 桌宠单独记的人格 | `pet_persona_id` | 字符串 | 自动检测 | 独立记录时的人格，留空自动补充「-桌宠」 |
| 记忆存到哪个会话（高级） | `default_session_id` | 字符串 | 自动检测 | 会话标识，留空自动挑记忆最多的历史会话 |
| 让桌宠读取群聊记忆 | `group_memory_read_enabled` | 布尔 | `false` | 关闭时只读自己私聊记忆、也不写群聊记忆；开启后才按下面的列表读取 |
| 私聊额外读取的群会话 | `read_scope_sessions` | 列表 | 无 | 一行一个群号；自动用当前适配器 ID 组装会话，旧文本配置仍兼容 |
| 消息从哪个平台进来（高级） | `default_adapter_id` | 字符串 | 无 | 高级项，多适配器互通时才填，一般留空 |
| 关键词子串增强 | `recall_keyword_boost` | 布尔 | `true` | 检索时额外走一条关键词子串，专有名词、工具名、型号、文件名这类稀有词靠它稳定召回；关掉后与没有这条时一致 |

另有 2 个隐藏项（旧映射两项）平时看不到，迁移场景才会出现，正常不用管。

「谁是谁」一行一个人，写成 `QQ号=名字,别名1,别名2`；等号前面是 QQ 号，后面第一段是记忆系统面板显示名，其余都算别名。示例：`10000=小明,小明明`。不填或留空面板只会显示 QQ 号。

纯桌宠（不接任何平台）时，图谱里的人物节点名取自「你的称呼」和「桌宠的称呼」，不用账号映射。

纯桌宠模式下，接 QQ 才用的项会被隐藏，隐藏期间不生效，值保留。

### 外部机器连接

默认只监听 `127.0.0.1`，其他机器上的桌宠连不上。需要跨机使用时：

1. 把「监听地址」改成 `0.0.0.0` 并保存；
2. **同时设置「校验 token」**（绑 `0.0.0.0` 且 token 为空等于把记忆 API 开放给整个局域网）；
3. 桌宠扩展的「AstrBot 的地址」填这台机器的局域网 IP + 端口，token 填同一串。

### 让私聊读到群聊记忆

群聊记忆按群会话隔离，私聊默认读不到。需要让桌宠在私聊里也能想起某个群：

1. 在 AstrBot 面板的桥接插件配置里找到「私聊里额外读取哪些群」；
2. 点「+」一行加一个群号（只填数字即可，例如 `123456789`；也可以填完整 `<适配器>:GroupMessage:<群号>`）；
3. 保存即生效（该值在桥接启动时读取，改完若没生效就重载一次插件）。

群聊查询仍只读当前群，不会被这个配置带出其他群；它只影响私聊/桌宠侧的检索范围。

### 常见误解

- **「记忆存到哪个会话」只影响桌宠那份记忆。** QQ 群聊和私聊仍按上游自己的会话规则走。
- **下拉里出现多个候选是历史会话，不是重复项。** 它们包括改名前的旧适配器名和机器人自己的号；通常留空，让插件自动挑记忆条数最多的会话。
- **「谁是谁」里第一段是显示名，后面都是别名。** 一个号只有一个主名字，别名只是辅助、可挂多个、不会替换主名字；这一项只对桌宠通道生效，QQ 侧走上游自己的用户身份别名。
- **接 QQ 时「你的称呼」不参与显示。** 以「谁是谁」为准；未登记的号显示数字本身。
- **`default_adapter_id` 只在接入第二个适配器并启用互通时才需要填。** 单适配器留空即可。
- **选「纯桌宠」时后面那些平台项会消失。** 它们只对 QQ 通道有意义；想看回来把「使用场景」切回「接入 AstrBot」。

## 常见排障

### 1. 面板保存后像没生效，重启才生效？

先检查 AstrBot 的 `data/plugins/` 目录里是否还有一份同名插件副本（例如 `astrbot_plugin_coopanion_livingmemory_backup`）。有副本时，面板保存会落到副本上，主插件看起来就没变化。把副本移出 `data/plugins/` 再保存即可。插件启动时如果发现同名目录，也会给出提示。

### 2. 人格改名后桌宠“失忆”，或面板填了不存在的人格？

先确认「记忆存给哪个人格」与 AstrBot 当前使用的人格一致。人格改名后，旧记忆仍挂在旧人格下，桌宠会查不到；可以在 QQ 里直接对机器人发这条命令迁移：

```text
/lmbridge migrate-persona 人格A 人格B apply
```

把示例里的「人格A」「人格B」换成你的旧人格和新人格；不带 `apply` 时只预览影响条数。填了一个不存在的人格时，插件会自动回退到当前人格，并在状态里给出告警。

### 3. `bot_qq` 填成了自己的 QQ 号？

插件会检测到这种“自指”配置：它会提示告警，并忽略这个错误值、改用自动识别。请把「桌宠的 QQ 号」改成桌宠账号本身。

---
## 依赖与参考

| 上游项目 | 作者 | 用途 | 许可 |
| :--- | :--- | :--- | :--- |
| [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | lxfight | 记忆库、知识图谱与会话蒸馏引擎；本插件通过其公开接口完成检索和写入 | 见上游仓库 |

## 许可证

[MIT License](LICENSE) © 2026 HirannU
