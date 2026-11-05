# astrbot_plugin_coopanion_livingmemory

> **AstrBot 插件**：把 AstrBot 上的 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 长期记忆与知识图谱，通过本地 HTTP 桥无缝接入 [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) 桌面宠物。

QQ 上聊过的事桌宠记得，桌宠这儿说过的约定 QQ 那边也知道——因为两端读写的是同一份记忆库与知识图谱。

> ⚠️ **配套说明**：
> - 本插件**运行在 AstrBot** 侧，负责把桌宠发来的请求转接给 `astrbot_plugin_livingmemory` 引擎；
> - 桌宠侧必须成对安装配套扩展：[`cortico-world-astrbot-livingmemory`](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory)；
> - 两者通过同一套本地 HTTP 接口交互，**必须成对安装运行**。

---

## 架构与分工

```
Coopanion（桌面宠物）
  │  桌宠侧扩展：mem_search / mem_record + 对话流捕获推送
  ▼
cortico-world-astrbot-livingmemory（桌宠侧扩展，TypeScript）
  │  本地 HTTP（默认 127.0.0.1:6198）
  ▼
astrbot_plugin_coopanion_livingmemory（本插件，Python）   ← 专注转接与身份映射
  │  直接调用 LivingMemory 内部引擎与反思服务
  ▼
astrbot_plugin_livingmemory（记忆库 + 向量检索 + 知识图谱 + 会话蒸馏）
```

本插件不自行存储数据、不做向量化，只负责把桌宠的 HTTP 请求转换为对 LivingMemory 的调用，并将检索结果与记忆沉淀送回。记忆的所有持久化、三路融合召回与图谱节点完全由 `astrbot_plugin_livingmemory` 托管。

---

## 配套插件

本插件必须与配套的桌宠端扩展成对安装：

- **[cortico-world-astrbot-livingmemory](https://github.com/HirannU-OVO/cortico-world-astrbot-livingmemory)**：运行在 Coopanion 桌宠侧的 World 扩展，向模型暴露记忆检索/沉淀工具，并把桌宠对话流推送过来做后台蒸馏。

两者通过本地 HTTP 接口对接（默认 `127.0.0.1:6198`），接口协议一致、缺一不可。

---

## 安装与快速上手

### 1. 前置准备
确保 AstrBot 实例中已安装并正常运行上游核心插件：
- [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（记忆与图谱引擎）

### 2. 安装本插件
将本仓库克隆或解压至 AstrBot 的插件目录：
```bash
git clone https://github.com/HirannU-OVO/astrbot_plugin_coopanion_livingmemory.git data/plugins/astrbot_plugin_coopanion_livingmemory
```
重启 AstrBot。

### 3. 配置身份与会话
在 AstrBot 管理面板中打开本插件配置页，配置以下关键项：

| 配置项 | 必填 | 作用与格式 | 示例 |
| :--- | :---: | :--- | :--- |
| `default_session_id` | 是 | 统一消息会话标识 umo，使桌宠记忆与私聊记忆归属同一主体 | `persona名:FriendMessage:你的QQ号` |
| `bot_identity` | 是 | 机器人账号身份定义（`id` 填机器人 QQ 号，`name` 填称呼） | `{"id":"10000","name":"大肥鱼","aliases":["小鲸鱼"]}` |
| `user_display_names` | 否 | 参与者数字 ID 到自然名称的映射 | `{"10000":"你的昵称"}` |

> 提示：若 `default_session_id` 留空，将退化为全局独立会话，导致桌宠记忆与 QQ 会话记忆相互隔离。

---

## 配置参数速查

所有配置项均可在 AstrBot 管理面板可视化修改：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `host` | `127.0.0.1` | 监听地址（仅限本机安全通信） |
| `port` | `6198` | HTTP 服务监听端口，需与桌宠扩展配置一致 |
| `default_session_id` | `""` | 默认会话 umo |
| `default_persona_id` | `default` | 默认人格标识 |
| `default_adapter_id` | `""` | 适配器标识 |
| `bot_identity` | `{}` | 机器人身份定义，`id` 务必填 QQ 号，避免图谱分裂为多个节点 |
| `user_display_names` | `{}` | 用户昵称字典 |
| `user_aliases` | `{}` | 用户别名列表 |
| `auth_token` | `""` | 可选请求鉴权令牌，配置后需在桌宠侧填入相同 Token |
| `enable_auto_graph_metadata` | `true` | 是否在沉淀记忆时自动提取话题并建立图谱关联 |
| `enable_auto_reflection` | `true` | 是否根据对话轮数自动触发 LivingMemory 的会话总结蒸馏 |

---

## 接口说明 (HTTP API)

默认服务地址为 `http://127.0.0.1:6198`。若配置了 `auth_token`，所有 `/api/*` 请求均需附带请求头 `Authorization: Bearer <token>`。

- `GET /api/health`：服务健康度与 LivingMemory 引擎连通性检查。
- `POST /api/memory/search`：三路融合检索（向量语义 + BM25 + 知识图谱）。
- `POST /api/memory/add`：显式沉淀单条事实或偏好。
- `POST /api/memory/batch_add`：批量迁移或导入记忆条目。
- `POST /api/memory/chat`：同步桌宠对话流水，驱动后台反思蒸馏。
- `POST /api/memory/reflect`：手动触发一次会话蒸馏结算。

---

## 常见排障

1. **图谱中同一个人分裂成两个节点**：
   - 检查 `bot_identity.id`，必须为纯数字 QQ 号；其他称呼请填入 `aliases` 数组中。
2. **桌宠查不到历史记忆**：
   - 核对 `default_session_id` 是否与 QQ 侧会话的 umo 格式完全一致。
3. **接口返回 401 Unauthorized**：
   - 检查本插件的 `auth_token` 与桌宠端配置的「鉴权令牌」是否匹配。

---

## 参考与致谢

本项目为原创实现。下表列明所依赖/参考的项目、参考内容及其边界：

| 项目 | 许可 | 关系与参考内容 |
| :--- | :--- | :--- |
| [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | 见上游仓库 | 记忆库/图谱引擎依赖：本插件经其公开插件实例调用检索与写入接口，未复制其代码 |

> **边界声明**：本仓库不包含任何第三方项目的源代码；与上游的交互均通过公开插件 API 或本地 HTTP 接口完成。若权利人认为有不妥之处，欢迎提 issue 沟通处理。

---

## 许可证

[MIT License](LICENSE) © 2026 HirannU
