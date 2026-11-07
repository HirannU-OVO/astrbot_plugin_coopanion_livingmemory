# LivingMemory 桥接（astrbot_plugin_coopanion_livingmemory）

让 QQ 上的 AstrBot 和桌面上的桌宠共用同一份长期记忆。

> ⚠️ **配套说明**
> - 本插件运行在 **AstrBot** 侧，负责把桌宠发来的请求转接给上游记忆插件；
> - 桌宠侧必须成对安装配套扩展：[cortico-world-astrbot-livingmemory](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory)；
> - 两个插件通过本地 HTTP 接口交互，**必须成对安装运行**。

---

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

| 项目 | 位置 | 作用 |
| :--- | :--- | :--- |
| [Cortico 桥](https://github.com/as124da1231/astrbot_plugin_cortico_bridge)（`astrbot_plugin_cortico_bridge`） | AstrBot 插件 | 管「对话」：让 Cortico/桌宠人格直接在 QQ 里说话，AstrBot 只当手脚和账号 |
| [cortico-world-astrbot](https://github.com/as124da1231/cortico-world-astrbot) | 桌宠扩展 | 和上一行成对，把桌宠人格接到 QQ |
| [本插件](https://github.com/HirannU-OVO/astrbot_plugin_coopanion_livingmemory) + [桌宠扩展](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory) | 本仓库 + 桌宠扩展 | 管「记忆」：把桌宠和 QQ 的记忆并到同一套 LivingMemory |

上面两项是一对，负责「对话」；本插件这一对负责「记忆」。两者功能不重叠，**可以同时装**：装了 Cortico 桥，QQ 那头的回答由 Cortico 人格来出，记忆照样走本插件。

本插件不依赖 Cortico 桥和 cortico-world-astrbot；只有想让桌宠人格在 QQ 里说话时才需要装它们。

---

## 安装与快速上手

### 1. 安装前置插件

先在 AstrBot 中安装并启用上游记忆插件：

- [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)

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
| `POST /api/memory/search` | 检索长期记忆（语义 + 关键词 + 知识图谱） |
| `POST /api/memory/add` | 写入一条记忆 |
| `POST /api/memory/batch_add` | 批量写入记忆（迁移/导入用） |
| `POST /api/memory/chat` | 接收桌宠对话，交给记忆插件做后台总结 |
| `POST /api/memory/reflect` | 手动触发一次会话总结 |

如果配置了「校验 token」，所有 `/api/*` 请求都需要带上 `Authorization: Bearer <你的 token>`。

---

## 配置参数速查

| 面板显示名 | 说明 |
| :--- | :--- |
| 监听端口 | 桌宠桥的监听端口，两边必须一致 |
| 校验 token | 和桌宠扩展填同一串，如 `123456789` |
| 使用场景 | 接入 QQ 或纯桌宠，按你实际用法选 |
| 记忆共享模式 | 一起记＝两边共用一套；桌宠单独记＝各记各的 |
| 桌宠的 QQ 号 | 机器人的 QQ 号，用来在图谱里认出它 |
| 桌宠的称呼 | 机器人的称呼 |
| 桌宠的其他叫法 | 一行一个，想加几个加几个 |
| 你的称呼 | 桌宠这边怎么称呼你，只在纯桌宠下显示与生效 |
| 谁是谁 | 一行一个 QQ 号=名字,别名 |
| 自动总结桌宠对话 | 桌宠对话自动整理成记忆 |
| 自动建立记忆关联 | 自动补记忆之间的关联 |
| 记忆存给哪个人格 | 留空自动识别，一般不用填 |
| 桌宠单独记的人格 | 留空自动用上面人格加「-桌宠」 |
| 记忆存到哪个会话（高级） | 会话标识，如 aiocqhttp:FriendMessage:123456789 |
| 消息从哪个平台进来（高级） | 多适配器互通时才填，默认留空 |

另有 2 个隐藏项（旧映射两项）平时看不到，迁移场景才会出现，正常不用管。

「谁是谁」一行一个人，写成 `QQ号=名字,别名1,别名2`；等号前面是 QQ 号，后面第一段是记忆系统面板显示名，其余都算别名。示例：`123456789=小明,小明明`。不填或留空面板只会显示 QQ 号。

纯桌宠（不接任何平台）时，图谱里的人物节点名取自「你的称呼」和「桌宠的称呼」，不用账号映射。

纯桌宠模式下，接 QQ 才用的项会被隐藏，隐藏期间不生效，值保留。

### 常见误解

- **「记忆存到哪个会话」只影响桌宠那份记忆。** QQ 群聊和私聊仍按上游自己的会话规则走。
- **下拉里出现多个候选是历史会话，不是重复项。** 它们包括改名前的旧适配器名和机器人自己的号；通常留空，让插件自动挑记忆条数最多的会话。
- **「谁是谁」里第一段是显示名，后面都是别名。** 一个号只有一个主名字，别名只是辅助、可挂多个、不会替换主名字；这一项只对桌宠通道生效，QQ 侧走上游自己的用户身份别名。
- **接 QQ 时「你的称呼」不参与显示。** 以「谁是谁」为准；未登记的号显示数字本身。
- **`default_adapter_id` 只在接入第二个适配器并启用互通时才需要填。** 单适配器留空即可。
- **选「纯桌宠」时后面那些平台项会消失。** 它们只对 QQ 通道有意义；想看回来把「使用场景」切回「接入 QQ」。

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

| 上游项目 | 用途 | 许可 |
| :--- | :--- | :--- |
| [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | 记忆库、知识图谱与会话蒸馏引擎；本插件通过其公开接口完成检索和写入 | 见上游仓库 |

## 许可证

[MIT License](LICENSE) © 2026 HirannU
