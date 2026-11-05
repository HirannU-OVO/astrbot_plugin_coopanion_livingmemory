# 更新日志

本文件记录「LivingMemory 桌宠直连桥接 (astrbot_plugin_coopanion_livingmemory)」插件的版本变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-05

> 首个正式版：把 AstrBot 的 LivingMemory 长期记忆与知识图谱，通过本地 HTTP 桥接给 Coopanion 桌宠。

### 新增

- **本地 HTTP 服务**（aiohttp，默认 `127.0.0.1:6198`）：提供 `health` / `search` / `add` / `batch_add` / `chat` / `reflect` 六个端点，按 `session_id` / `persona_id` 隔离。
- **记忆反思**：`/api/memory/chat` 写入后按 LivingMemory 的 `summary_trigger_rounds` 阈值自动判定并调度后台蒸馏，产出记忆、记忆原子与图谱；`/api/memory/reflect` 可手动触发同一次判定。
- **图谱关联补全**：入库前自动补齐 `canonical_summary` / `key_facts` / `participant_identities` / `topics`，为记忆建立「话题—事实」「人物—事实」「人物共现」三类关联（实测同一段文本从 1 节点 0 边变为 4~7 节点 5~11 边）。
- **身份对齐**：bot 的 `identity_key` 固定用 QQ 号、`display_name` 用称呼，其他叫法登记为别名；用户称呼走 `user_display_names`，避免同一个人被拆成两个节点。
- **可选鉴权**：配置 `auth_token` 后所有 `/api` 端点要求 `Authorization: Bearer`。
- **面板配置**：基础 8 项（身份 / 令牌 / 端口）+ 高级 5 项（会话 / 人格 / 适配器 / 两个开关）；字典类配置改为纯文本写法（`user_names_text` 等「QQ号=名字」格式），旧 dict 配置仍兼容。

### 修复

- 插件加载失败（`KeyError: 'items'`）：`bot_identity` 从 `object` 改为 `dict`。
- `self.self.user_alias_cfg` 笔误导致加载失败。
- 升级易断：插件实例、记忆引擎、对话管理器、反思模块全部改为多路径探测，缺失时回执直接指出缺哪个符号，不再笼统报 503。
- `port` 配置项类型改为 `int`，对齐 AstrBot 配置校验器。

### 优化

- 开源脱敏：默认值不含任何真实账号、人名与本机路径；未配置身份时用 persona 兜底。
- `/api/health` 增加 `reflection_available` 与 `auto_reflection`，便于桌宠侧自检。
- README 补齐完整配置表、六个端点文档与常见排障；移除含本机路径与账号的一次性迁移脚本。
