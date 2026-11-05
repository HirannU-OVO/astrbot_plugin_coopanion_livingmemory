# astrbot_plugin_coopanion_livingmemory

把 AstrBot 的 LivingMemory 长期记忆接到 Coopanion 桌宠上。

QQ 上聊过的事桌宠记得，桌宠这儿说过的约定 QQ 那边也知道——因为两边读写的是同一份记忆库与知识图谱。

> **适用范围**：本插件**只能运行在 AstrBot** 上——它直接调用 AstrBot 的插件 API 与 `astrbot_plugin_livingmemory` 的内部引擎，不跨平台。桥暴露的 HTTP 接口（`/api/memory/*`）本身是通用 JSON 协议，理论上任何本机程序都能对接；但配套的桌宠端扩展目前只有 Coopanion 版本。

> 桌宠侧还需要装对应扩展 `cortico-world-astrbot-livingmemory`，两个包按同一套协议对接，**必须成对安装**。

---

## 分工

```
Coopanion（桌宠）
  │  桌宠侧扩展：mem_search / mem_record + 对话流推送
  ▼
cortico-world-livingmemory（桌宠侧，TypeScript）
  │  本地 HTTP，默认 127.0.0.1:6198
  ▼
astrbot_plugin_coopanion_livingmemory（本插件，Python）   ← 只做转接
  │  直接调用 LivingMemory 的引擎接口
  ▼
astrbot_plugin_livingmemory（记忆库 + 知识图谱 + 会话蒸馏）
```

本插件不存数据、不做检索，只把桌宠的请求转成 LivingMemory 的调用，再把结果送回去。记忆的存储、向量检索、图谱与总结全在 LivingMemory 里。

---

## 安装

1. 已装好 `astrbot_plugin_livingmemory`
2. 把本插件放进 AstrBot 的 `data/plugins/`，重启
3. 打开插件配置页填好这两项（**必须填对**）：

| 配置 | 填什么 | 例 |
| :--- | :--- | :--- |
| `default_session_id` | 会话 umo（AstrBot 的统一消息来源标识） | `persona名:FriendMessage:你的QQ号` |
| `bot_identity` | 桌宠账号身份（`id` 用 QQ 号、`name` 用称呼） | `{"id":"10000","name":"小助手"}` |

留空也能跑，但 `default_session_id` 留空会退化到通用占位，桌宠的记忆与 QQ 侧的记忆就分成两份了。

---

## 配置

全部在 AstrBot 的插件配置页里改（无需手改文件）：

| 配置 | 默认 | 说明 |
| :--- | :--- | :--- |
| `host` | `127.0.0.1` | 监听主机，只给本机桌宠用就保持默认 |
| `port` | `6198` | 监听端口，改了要与桌宠侧 `bridgeUrl` 一致 |
| `default_session_id` | 空 | 桌宠记忆归属的会话 umo。留空会退化到通用占位、与 QQ 侧记忆分离 |
| `default_persona_id` | `default` | 记忆的人格标签；AstrBot 未配人格时仅作分类 |
| `default_adapter_id` | 空 | 平台适配器标识，可留空 |
| `bot_identity` | `{}` | 桌宠账号身份：`{"id":"<QQ号>","name":"<称呼>","aliases":["<其他叫法>"]}`。id 必须填 QQ 号，名字放 aliases，否则会被拆成两个人物节点 |
| `user_display_names` | `{}` | 参与者称呼映射：`{"<QQ号>":"<名字>"}`，记忆里就会写名字而不是数字 ID |
| `user_aliases` | `{}` | 对方的其他叫法：`{"<QQ号>":["小名"]}` |
| `auth_token` | 空 | 可选鉴权。填了之后所有 /api 请求都要带 `Authorization: Bearer <token>`，桌宠侧「鉴权令牌」填同值 |
| `enable_auto_graph_metadata` | `true` | 入库时自动补齐图谱元数据（话题 / 参与者 / 摘要）。关掉后记忆之间不会建立关系 |
| `enable_auto_reflection` | `true` | 桌宠对话是否自动触发记忆蒸馏（每 N 轮总结一次） |

---

## HTTP 接口

默认监听 `http://127.0.0.1:6198`。配了 `auth_token` 时所有 `/api/*` 都要求 `Authorization: Bearer <token>`。

### `GET /api/health`

```json
{
  "ok": true,
  "version": "v0.1.0-dev.5",
  "livingmemory_connected": true,
  "reflection_available": true,
  "auto_reflection": true,
  "default_session_id": "persona:FriendMessage:10000",
  "stats": { "total_memories": 121, "graph_nodes": 171 }
}
```

`reflection_available` 为 false 说明拿不到 LivingMemory 的记忆反思模块（通常是插件升级改了内部结构）。

### `POST /api/memory/search`

```json
{ "query": "表情包发语音的规矩", "limit": 5, "session_id": "可选", "persona_id": "可选" }
```

返回 `{ ok, count, memories: [{ id, content, score, metadata }] }`，三路融合（向量 + BM25 + 图谱）排序。

### `POST /api/memory/add`

```json
{
  "content": "主人要求发语音时不要带表情包标记",
  "importance": 0.7,
  "topics": ["表情包规范", "语音"],
  "participants": [ { "identity_key": "10000", "display_name": "小助手", "is_bot": true } ]
}
```

`topics` 与 `participants` 可省略；省略时由插件按 `bot_identity` / `user_display_names` 与文本启发式补齐。否则这条记忆只会挂一个孤立的事实节点，不与其他记忆相连。

### `POST /api/memory/batch_add`

```json
{ "memories": [ { "content": "…", "importance": 0.6, "topics": ["…"], "participants": [] } ] }
```

逐条写入，返回 `{ ok, total, added_count, added_ids, errors }`。用于批量迁移。**没有内容去重**——重复提交会重复入库。

### `POST /api/memory/chat`

```json
{ "turns": [ { "role": "user", "content": "…", "timestamp": 1793849000 } ], "session_id": "可选" }
```

把对话轮次写进 LivingMemory 的会话窗口。写入后若 `enable_auto_reflection` 打开，会自动做一次蒸馏判定。

### `POST /api/memory/reflect`

```json
{ "session_id": "可选" }
```

单独触发一次蒸馏判定：未总结轮数达到 `reflection_engine.summary_trigger_rounds`（默认 10 轮）时排后台总结任务，产出记忆 + 记忆原子 + 图谱。

---

## 几个说明

**记忆不是每句都存**
蒸馏由 LivingMemory 的 LLM 按轮次批量执行。想立刻记下来，让模型调 `mem_record`。

**同一个人变成两个节点**
`bot_identity.id` 必须是 QQ 号，`name` 是称呼，其他叫法放 `aliases`。把名字当 id 用就会分裂。

**升级后功能消失**
插件用多路径探测去找 LivingMemory 的引擎与反思模块。真拿不到时 `/api/health` 的 `reflection_available` 会是 false，`/api/memory/reflect` 会直接告诉你缺哪个符号。

**安全**
桥是明文 HTTP，默认只监听 127.0.0.1。跨机器用务必设 `auth_token`，并尽量放在内网或加一层 TLS 反代。

---

## 许可

[MIT License](LICENSE) © 2026 HirannU
