# 更新日志

本文件记录「LivingMemory 桌宠直连桥接 (astrbot_plugin_coopanion_livingmemory)」插件的版本变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0-dev.7] - 2026-11-05

### 优化
- 配置面板恢复为完整两级：基础 8 项（身份/令牌/端口）+ 高级 5 项（会话/人格/适配器/两个开关），不再裁剪——上一版只留 5 项，像自动检测失败的兜底项被藏起来，用户不知道配置存在。
- 字典类配置全部改为纯文本写法，不再要求用户写 JSON：`user_names_text`（QQ号=名字,QQ号=名字）、`user_aliases_text`（QQ号=别名1/别名2）、`bot_aliases_text`（叫法,叫法）；旧 dict 配置仍然兼容，文本项优先。

## [0.1.0-dev.6] - 2026-11-05

### 优化
- 配置只剩 5 项，全部是纯文本/数字（不再是 JSON 括号格式）：桌宠 QQ 号、桌宠称呼、你的称呼、鉴权令牌、端口。
- 会话与双方身份改为**自动检测**：会话从 LivingMemory 的统计里取，账号与昵称从 conversations.db 的历史消息里读；三项留空即可用。
- 其余配置（host / 人格 / 适配器 / 名字别名 / 两个开关）不再暴露在面板，走默认值，需要时可手改配置文件。

### 修复
- 修正 `self.self.user_alias_cfg` 的笔误（上一版会导致插件加载失败）。

## [0.1.0-dev.5] - 2026-11-05

### 优化
- 开源脱敏：所有默认值不再硬编码账号与人名；未配置身份时用 persona 兜底，不再指向任何具体 QQ 号。
- README 补齐完整配置表（含 bot_identity / user_display_names / auth_token / 两个开关）与「与桌宠侧配对」说明。
- 移除一次性迁移脚本（含本机路径与账号），开源包只留插件本体。
- README 重写：去掉「高性能低延迟」「优雅解耦」等形容，改为务实的分工说明 + 六个 HTTP 端点文档（补上原先缺的 `/api/memory/reflect`）+ 四个常见问题。
- LICENSE 作者改为 HirannU。

## [0.1.0-dev.4] - 2026-11-05

### 修复
- 插件加载失败（`KeyError: 'items'`）：`bot_identity` 用了 `object` 类型，AstrBot 会把它当配置分组去读 `items`；改为 `dict` 后正常加载。
- 身份对齐：bot 的 identity_key 固定用 QQ 号、display_name 用称呼，其他叫法登记为别名；用户的称呼走 user_display_names。避免同一个人被拆成两个节点。

## [0.1.0-dev.3] - 2026-11-05

### 新增
- 端点 `/api/memory/reflect`：按 LivingMemory 的规则（`summary_trigger_rounds`）对桌宠会话做一次蒸馏判定，达标即调度后台总结任务，产出记忆 + 记忆原子 + 图谱。
- `/api/memory/chat` 写入后自动触发同一次判定（开关 `enable_auto_reflection`）——桌宠对话从"只留历史"变成"能长成长期记忆"。
- 可选鉴权 `auth_token`：配置后所有 /api 端点要求 `Authorization: Bearer`，桌宠侧配同一个值。

### 修复
- 升级易断：插件实例、记忆引擎、对话管理器、记忆反思模块全部改为**多路径探测**（属性改名/层级变化时自动换路径），拿不到时回执直接指出缺哪个符号，不再是笼统的 503。
- `/api/health` 增加 `reflection_available` 与 `auto_reflection`，便于桌宠侧自检。

## [0.1.0-dev.2] - 2026-11-05

### 修复
- 导入的记忆全部变成孤立条目：入库前自动补齐图谱抽取所需的 `canonical_summary` / `key_facts` / `participant_identities` / `topics`，记忆之间重新建立「话题—事实」「人物—事实」「人物共现」三类关联（实测同一段文本从 1 节点 0 边变为 4~7 节点 5~11 边）。
- 对话流注入的发送者不再硬编码：改为从 session 解析双方身份，可用 `user_display_names` 配置展示名。

### 优化
- `/api/memory/add` 与 `/api/memory/batch_add` 支持调用方显式传入 `topics` / `key_facts` / `participants`，优先于自动抽取。
- 自动提词只收文本中重复出现的片段，避免往图谱塞「人今」「天说」这类碎片节点。
- 新增开关 `enable_auto_graph_metadata`，关闭后保持旧行为（记忆不建关系）。

## [0.1.0-dev.1] - 2026-11-05

### 新增
- 初始正式版本发布：基于 aiohttp.web 构建本地极速轻量 HTTP 服务（默认端口 127.0.0.1:6198）。
- 端点 `/api/health`：用于服务健康探针、LivingMemory 引擎在线状态检测与记忆数统计。
- 端点 `/api/memory/search`：提供毫秒级多路混合语义与图谱检索接口，支持按 session_id/persona_id 隔离与召回。
- 端点 `/api/memory/add`：支持显式原子记忆的实时存入与图谱关联构建。
- 端点 `/api/memory/batch_add`：支持批量记忆导入，专供数据平滑迁移与初始化同步。
- 端点 `/api/memory/chat`：接收桌宠端日常对话轮次（对齐 ConversationManager 格式），纳入会话消息缓冲以供反思与知识蒸馏。
- 提供数据迁移脚本 `scripts/migrate_coopanion_memories.py`，已完整迁移 822 条旧版事实记忆至 LivingMemory。

### 修复
- 修正 `_conf_schema.json` 中 `port` 配置项类型定义为 `int`，对齐 AstrBot 核心配置校验器规范。
