"""bridge_server.py - LivingMemory HTTP Bridge Server
提供本地 REST API 端点供 Coopanion 桌宠扩展进行高速直连交互
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web
from astrbot.api import logger
from astrbot.api.star import Context


class LivingMemoryBridgeServer:
    def __init__(self, context: Context, config: dict[str, Any]):
        self.context = context
        self.config = config
        self.host = config.get("host", "127.0.0.1")
        self.port = int(config.get("port", 6198))
        # 开源默认值不带任何真实账号：留空时先自动检测，检测不到才用占位
        self.default_session_id = str(
            config.get("default_session_id") or ""
        ).strip() or "coopanion:default"
        self.default_persona_id = str(config.get("default_persona_id") or "").strip() or "default"
        self.default_adapter_id = str(config.get("default_adapter_id") or "").strip()
        # 参与者显示名映射（identity_key → 展示名），用于把 session 里的 ID 变成可读的人名
        # 称呼映射：面板用纯文本（QQ号=名字），旧 dict 配置仍然兼容；文本项优先
        self.user_display_names: dict[str, str] = {
            **dict(config.get("user_display_names") or {}),
            **self._parse_kv(str(config.get("user_names_text") or "")),
        }
        # 桌宠（bot）的身份：id 用账号号、name 用称呼，其余叫法放 aliases。
        # 名字变体不登记为别名，LivingMemory 会把同一个人拆成两个人物节点。
        bot_cfg = config.get("bot_identity") or {}
        # 面板只让填纯文本（不填 JSON）：bot_qq / bot_name / user_name
        self._cfg_bot_qq = str(config.get("bot_qq") or "").strip()
        self._cfg_bot_name = str(config.get("bot_name") or "").strip()
        self._cfg_user_name = str(config.get("user_name") or "").strip()
        # 未配置时用 persona 作为身份键的兜底，不再硬编码任何账号
        self.bot_sender_id = self._cfg_bot_qq or str(bot_cfg.get("id") or "").strip() or self.default_persona_id
        self.bot_display_name = (
            self._cfg_bot_name
            or str(bot_cfg.get("name") or "").strip()
            or self.default_persona_id
        )
        self.bot_aliases: list[str] = [
            str(a).strip()
            for a in (bot_cfg.get("aliases") or [])
            if str(a).strip() and str(a).strip() != self.bot_display_name
        ]
        for alias in str(config.get("bot_aliases_text") or "").split(","):
            alias = alias.strip()
            if alias and alias != self.bot_display_name and alias not in self.bot_aliases:
                self.bot_aliases.append(alias)
        # 用户一侧的别名：桌宠在桌面上怎么称呼用户
        self.user_alias_cfg: dict[str, list[str]] = {
            **dict(config.get("user_aliases") or {}),
            **self._parse_alias_text(str(config.get("user_aliases_text") or "")),
        }
        # 自动检测只尝试一次（留空项才检测），结果缓存在实例上
        self._auto_ready = False
        self.enable_auto_graph_metadata = bool(
            config.get("enable_auto_graph_metadata", True)
        )
        # 桌宠对话是否自动触发 LivingMemory 的记忆蒸馏（总结 + 原子 + 图谱）
        self.enable_auto_reflection = bool(
            config.get("enable_auto_reflection", True)
        )
        # 可选 token 鉴权：留空 = 不校验（仅本机 127.0.0.1 时够用）
        self.auth_token = str(config.get("auth_token", "") or "").strip()

        self.app = web.Application(middlewares=[self._auth_middleware])
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._setup_routes()

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """配了 token 时校验 Authorization: Bearer <token>。"""
        if self.auth_token and request.path.startswith("/api/"):
            got = request.headers.get("Authorization", "")
            if got != f"Bearer {self.auth_token}":
                return web.json_response(
                    {"ok": False, "error": "unauthorized: token 不匹配"}, status=401
                )
        return await handler(request)

    def _setup_routes(self):
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_post("/api/memory/search", self.handle_search)
        self.app.router.add_post("/api/memory/add", self.handle_add)
        self.app.router.add_post("/api/memory/batch_add", self.handle_batch_add)
        self.app.router.add_post("/api/memory/chat", self.handle_chat)
        self.app.router.add_post("/api/memory/reflect", self.handle_reflect)

    def _get_livingmemory_plugin(self) -> Any | None:
        """获取 LivingMemory 插件实例（多路径探测，AstrBot 升级改字段时不至于全断）。"""
        try:
            star_meta = self.context.get_registered_star("astrbot_plugin_livingmemory")
            if star_meta is None:
                return None
            for attr in ("star_cls", "star", "inst", "instance", "plugin"):
                candidate = getattr(star_meta, attr, None)
                if candidate is not None and hasattr(candidate, "initializer"):
                    return candidate
            # 兜底：StarMetadata 自身可能就直接是插件对象
            if hasattr(star_meta, "initializer"):
                return star_meta
            logger.warning(
                "[LivingMemoryBridge] 找到 star 元数据但取不到插件实例，"
                f"可用属性: {[a for a in dir(star_meta) if not a.startswith('__')][:12]}"
            )
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 获取 LivingMemory 插件对象失败: {e}")
        return None

    def _get_memory_engine(self) -> Any | None:
        """获取 LivingMemory 引擎对象（多路径探测：插件内部属性改名时仍能找到）。"""
        plugin = self._get_livingmemory_plugin()
        return self._probe_member(plugin, ("memory_engine", "engine", "memory_service"), "add_memory")

    def _get_conversation_manager(self) -> Any | None:
        """获取 LivingMemory 对话管理器（多路径探测）。"""
        plugin = self._get_livingmemory_plugin()
        return self._probe_member(plugin, ("conversation_manager", "conv_manager"), "add_message")

    @staticmethod
    def _probe_member(plugin: Any, names: tuple[str, ...], required_method: str) -> Any | None:
        """在插件实例与 initializer 上按候选名查找带指定方法的成员。"""
        if plugin is None:
            return None
        holders = [getattr(plugin, "initializer", None), plugin]
        for holder in holders:
            if holder is None:
                continue
            for name in names:
                candidate = getattr(holder, name, None)
                if candidate is not None and hasattr(candidate, required_method):
                    return candidate
        return None

    # ------------------------------------------------------------------
    # 自动检测：让用户尽量不用填配置
    #
    # 会话：LivingMemory 的统计里已经按 session 列了记忆条数，取最多的那个。
    # 身份：conversations.db 的历史消息里就有双方的账号与昵称（is_bot_message 区分）。
    # 只在对应配置留空时检测，且整个实例只尝试一次；失败静默降级到占位值。
    # ------------------------------------------------------------------

    async def _ensure_auto(self) -> None:
        if self._auto_ready:
            return
        self._auto_ready = True
        try:
            if self.default_session_id == "coopanion:default":
                sid = await self._detect_session()
                if sid:
                    self.default_session_id = sid
                    logger.info(f"[LivingMemoryBridge] 自动检测会话: {sid}")
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 会话自动检测失败: {e}")
        try:
            if not (self._cfg_bot_qq and self._cfg_bot_name and self._cfg_user_name):
                found = self._detect_identities()
                if found:
                    bot_id, bot_name, user_id, user_name = found
                    if not self._cfg_bot_qq and bot_id:
                        self.bot_sender_id = bot_id
                    if not self._cfg_bot_name and bot_name:
                        self.bot_display_name = bot_name
                    if not self._cfg_user_name and user_id and user_name:
                        self.user_display_names.setdefault(user_id, user_name)
                    logger.info(
                        f"[LivingMemoryBridge] 自动检测身份: bot={bot_id}/{bot_name} user={user_id}/{user_name}"
                    )
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 身份自动检测失败: {e}")

    async def _detect_session(self) -> str | None:
        engine = self._get_memory_engine()
        if engine is None or not hasattr(engine, "get_statistics"):
            return None
        stats = await engine.get_statistics()
        sessions = (stats or {}).get("sessions") or {}
        if not isinstance(sessions, dict) or not sessions:
            return None

        def weight(item: tuple[str, Any]) -> float:
            v = item[1]
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict):
                return float(v.get("count") or v.get("total") or 0)
            return 0.0

        return max(sessions.items(), key=weight)[0]

    def _detect_identities(self) -> tuple[str, str, str, str] | None:
        """从 conversations.db 最近消息里读出 (bot_id, bot_name, user_id, user_name)。只读。"""
        import os
        import sqlite3

        from astrbot.api.star import StarTools

        data_dir = str(StarTools.get_data_dir("astrbot_plugin_livingmemory"))
        db = os.path.join(data_dir, "conversations.db")
        if not os.path.exists(db):
            return None
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            rows = con.execute(
                "SELECT sender_id, sender_name, is_bot_message FROM messages "
                "WHERE sender_id IS NOT NULL AND sender_id <> '' ORDER BY id DESC LIMIT 200"
            ).fetchall()
        finally:
            con.close()
        bot = user = None
        for sid, name, is_bot in rows:
            sid_s, name_s = str(sid or "").strip(), str(name or "").strip()
            if not sid_s:
                continue
            if is_bot and bot is None:
                bot = (sid_s, name_s or sid_s)
            elif not is_bot and user is None:
                user = (sid_s, name_s or sid_s)
            if bot and user:
                break
        if not bot and not user:
            return None
        b = bot or ("", "")
        u = user or ("", "")
        return (b[0], b[1], u[0], u[1])
    #
    # LivingMemory 的记忆只有一条产生路径：@filter.on_llm_response → handle_memory_reflection
    # → 每 N 轮把会话窗口交给 LLM 总结 → 同时写记忆与原子/图谱。
    # 桌宠的对话不经过 AstrBot 的 LLM 请求，所以桥写进 ConversationManager 只会留下历史，
    # 永远不会变成长期记忆。这里复刻 handle_memory_reflection 的"阈值判定 → 构造计划 →
    # 调度后台任务"三步，把捕获源换成桌宠对话。
    #
    # 依赖的是 MemoryReflection 的三个方法（其中两个不接收 event）：
    #   _read_session_revision / _build_reflection_plan_for_cursor / _schedule_reflection
    # 插件升级若改名，_get_reflection 的多路径探测会失败并在回执里指出缺哪个符号。
    # ------------------------------------------------------------------

    def _get_reflection(self) -> Any | None:
        """取 MemoryReflection 实例（多路径探测，兼容插件内部结构改名）。"""
        plugin = self._get_livingmemory_plugin()
        if not plugin:
            return None
        holders = []
        for holder_name in ("event_handler", "_event_handler"):
            holder = getattr(plugin, holder_name, None)
            if holder is not None:
                holders.append(holder)
            init = getattr(plugin, "initializer", None)
            if init is not None:
                nested = getattr(init, holder_name, None)
                if nested is not None:
                    holders.append(nested)
        for holder in holders:
            for attr in ("_memory_reflection", "memory_reflection", "reflection"):
                candidate = getattr(holder, attr, None)
                if candidate is not None and hasattr(
                    candidate, "_build_reflection_plan_for_cursor"
                ):
                    return candidate
        return None

    def _livingmemory_config_get(self, key: str, default: Any) -> Any:
        """读 LivingMemory 的配置项（读不到就用默认值）。"""
        plugin = self._get_livingmemory_plugin()
        for holder in (plugin, getattr(plugin, "initializer", None) if plugin else None):
            cm = getattr(holder, "config_manager", None) if holder else None
            if cm is not None and hasattr(cm, "get"):
                try:
                    return cm.get(key, default)
                except Exception:
                    continue
        return default

    async def _run_reflection(self, session_id: str) -> dict[str, Any]:
        """按 LivingMemory 的规则判断是否该总结；需要时调度后台任务。"""
        mr = self._get_reflection()
        cm = self._get_conversation_manager()
        if mr is None:
            return {
                "ok": False,
                "error": "拿不到 LivingMemory 的记忆反思模块（插件可能升级改名）",
                "hint": "检查 astrbot_plugin_livingmemory 版本；桥的 _get_reflection 需适配新的属性名",
            }
        if cm is None:
            return {"ok": False, "error": "ConversationManager 尚未就绪"}

        scope_mode = str(
            self._livingmemory_config_get("filtering_settings.memory_scope_mode", "legacy")
        ).casefold()
        if scope_mode not in ("legacy", "session"):
            return {
                "ok": False,
                "error": f"当前 memory_scope_mode={scope_mode}，桥只支持 legacy/session 两种作用域",
                "hint": "把 filtering_settings.memory_scope_mode 改为 legacy 或 session",
            }

        try:
            store = getattr(cm, "store", None)
            total = await store.get_message_count(session_id)
            last = await cm.get_session_metadata(session_id, "last_summarized_index", 0)
            pending = await cm.get_session_metadata(session_id, "pending_summary", None)
            threshold = int(
                self._livingmemory_config_get("reflection_engine.summary_trigger_rounds", 10)
            )
            unsummarized = max(0, (int(total) - int(last or 0)) // 2)
            if unsummarized < threshold:
                return {
                    "ok": True,
                    "scheduled": False,
                    "reason": f"未总结轮数 {unsummarized} < 阈值 {threshold}",
                    "total_messages": total,
                    "last_summarized_index": last,
                }
            revision = None
            reader = getattr(mr, "_read_session_revision", None)
            if callable(reader):
                try:
                    revision = await reader(session_id)
                except Exception:
                    revision = None
            plan = await mr._build_reflection_plan_for_cursor(
                session_id,
                int(total),
                int(last or 0),
                pending,
                revision,
                self.default_persona_id,
                session_id,
            )
            if plan is None:
                return {
                    "ok": True,
                    "scheduled": False,
                    "reason": "未生成总结计划（窗口消息不足或正在重试）",
                    "unsummarized_rounds": unsummarized,
                }
            await mr._schedule_reflection(plan)
            logger.info(
                f"[LivingMemoryBridge] 已为 {session_id} 调度记忆总结（{unsummarized} 轮）"
            )
            return {
                "ok": True,
                "scheduled": True,
                "unsummarized_rounds": unsummarized,
                "window": [plan.start_index, plan.end_index],
            }
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] 触发记忆总结失败: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _parse_kv(text: str) -> dict[str, str]:
        """解析「QQ号=名字,QQ号=名字」格式的纯文本为映射（支持中英文逗号/分号/换行）。"""
        import re as _re
        out: dict[str, str] = {}
        separators = ",，;；\n"
        for part in _re.split("[" + separators + "]", text or ""):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    out[k] = v
        return out

    @staticmethod
    def _parse_alias_text(text: str) -> dict[str, list[str]]:
        """解析「QQ号=别名1/别名2,QQ号=别名3」格式的纯文本为别名映射。"""
        import re as _re
        out: dict[str, list[str]] = {}
        separators = ",，;；\n"
        for part in _re.split("[" + separators + "]", text or ""):
            if "=" in part:
                k, v = part.split("=", 1)
                aliases = [a.strip() for a in _re.split(r"[/|]", v) if a.strip()]
                if k.strip() and aliases:
                    out[k.strip()] = aliases
        return out

    # ------------------------------------------------------------------
    # 图记忆元数据补齐
    #
    # LivingMemory 的图谱只有三条建边来源（graph_extractor._extract_legacy）：
    #   topics                  → topic -[describes]-> fact
    #   participant_identities  → person -[mentioned_in]-> fact，以及 person 之间 co_occurs_with
    # 桥原先只传 content/importance，这三样一个都没有 → 每条记忆只剩一个孤立 fact 节点，
    # 导入后「全部变成独立记忆」。这里在入库前补齐，保证每批记忆都能连上图。
    # 调用方显式传入时以调用方为准。
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str, limit: int = 8) -> list[str]:
        """按中英文句读切句，过短的碎片丢弃。"""
        import re

        parts = re.split(r"(?<=[。！？!?；;\n])|(?<=\.)\s+", text)
        out: list[str] = []
        for part in parts:
            s = part.strip(" \t\r\n。；;")
            if len(s) >= 2:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    _STOPWORDS = {
        "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "因为", "所以",
        "但是", "而且", "就是", "还是", "已经", "可以", "一下", "现在", "今天",
        "明天", "昨天", "时候", "自己", "觉得", "知道", "没有", "不是", "一个",
        "这样", "那样", "真的", "其实", "然后", "如果", "虽然", "不过", "还有",
    }

    @classmethod
    def _extract_topics(cls, text: str, limit: int = 3) -> list[str]:
        """轻量关键词兜底：CJK 2–4 字滑窗计频 + 停用词过滤。

        只收**在文本里重复出现**的片段：单次出现的滑窗多半是无意义碎片（「人今」「天说」），
        宁可少几条边也不往图谱里塞垃圾节点。准确的主题词应由调用方通过 topics 传入。
        """
        import re
        from collections import Counter

        cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", text)
        counter: Counter[str] = Counter()
        for run in cleaned.split():
            for size in (2, 3, 4):
                for i in range(0, max(0, len(run) - size + 1)):
                    gram = run[i : i + size]
                    if not re.fullmatch(r"[\u4e00-\u9fff]+", gram):
                        continue
                    if gram in cls._STOPWORDS:
                        continue
                    counter[gram] += 1
        picked: list[str] = []
        for gram, count in counter.most_common(limit * 6):
            if count < 2:
                break
            if any(gram in p or p in gram for p in picked):
                continue
            picked.append(gram)
            if len(picked) >= limit:
                break
        return picked

    def _participants_from_session(
        self, session_id: str, persona_id: str
    ) -> list[dict[str, Any]]:
        """从 session 解析参与者，供建 person 节点与边。

        bot 一侧固定用 bot_identity（账号号 + 人名），保证与历史数据里的人物节点同一个人；
        用户一侧取 session 里的数字账号，显示名走 user_display_names。
        """
        parts = [p for p in str(session_id or "").split(":") if p]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(key: str, name: str, is_bot: bool) -> None:
            k = key.strip()
            if not k or k in seen:
                return
            seen.add(k)
            aliases = list(self.bot_aliases) if is_bot else [
                str(a).strip()
                for a in (self.user_alias_cfg.get(k) or [])
                if str(a).strip()
            ]
            out.append(
                {
                    "identity_key": k,
                    "display_name": (name or "").strip() or k,
                    "sender_id": k,
                    "platform": parts[1] if len(parts) >= 2 else "aiocqhttp",
                    "aliases": [a for a in aliases if a and a != (name or "").strip()],
                    "is_bot": is_bot,
                }
            )

        # bot 身份：账号号作 identity_key，人名作 display_name，另两个叫法作别名
        add(
            self.bot_sender_id,
            self.user_display_names.get(self.bot_sender_id, self.bot_display_name),
            True,
        )
        for part in parts[1:]:
            if part.isdigit() and part != self.bot_sender_id:
                add(part, self.user_display_names.get(part, part), False)
        return out

    def _enrich_metadata(
        self,
        content: str,
        session_id: str,
        persona_id: str,
        metadata: dict[str, Any],
        topics: Any = None,
        key_facts: Any = None,
        participants: Any = None,
    ) -> dict[str, Any]:
        """补齐图记忆抽取所需元数据（显式传参 > 已有 metadata > 自动兜底）。"""
        md = dict(metadata or {})
        if not self.enable_auto_graph_metadata:
            return md

        if topics:
            md["topics"] = [str(t) for t in topics if str(t).strip()][:8]
        if key_facts:
            md["key_facts"] = [str(f) for f in key_facts if str(f).strip()][:12]
        if isinstance(participants, list) and participants:
            md["participant_identities"] = [
                p for p in participants if isinstance(p, dict)
            ]

        md.setdefault("canonical_summary", content[:400])
        md.setdefault("key_facts", self._split_sentences(content) or [content[:400]])
        md.setdefault("topics", self._extract_topics(content))
        md.setdefault(
            "participant_identities",
            self._participants_from_session(session_id, persona_id),
        )
        return md

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /api/health - 探针与状态"""
        await self._ensure_auto()
        engine = self._get_memory_engine()
        connected = engine is not None

        stats_data = {}
        if connected:
            try:
                stats = await engine.get_statistics()
                stats_data = stats if isinstance(stats, dict) else {}
            except Exception as e:
                stats_data = {"error": str(e)}

        return web.json_response(
            {
                "ok": True,
                "plugin": "astrbot_plugin_livingmemory_bridge",
                "version": "v0.1.0-dev.7",
                "livingmemory_connected": connected,
                "reflection_available": self._get_reflection() is not None,
                "auto_reflection": self.enable_auto_reflection,
                "default_session_id": self.default_session_id,
                "default_persona_id": self.default_persona_id,
                "stats": stats_data,
            }
        )

    async def handle_search(self, request: web.Request) -> web.Response:
        """POST /api/memory/search - 多路混合语义与图谱检索"""
        await self._ensure_auto()
        engine = self._get_memory_engine()
        if not engine:
            return web.json_response(
                {"ok": False, "error": "LivingMemory 引擎尚未就绪或不可用"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的 JSON 请求体"}, status=400
            )

        query = data.get("query", "").strip()
        if not query:
            return web.json_response(
                {"ok": False, "error": "query 不能为空"}, status=400
            )

        limit = int(data.get("limit", 5))
        session_id = data.get("session_id") or self.default_session_id
        persona_id = data.get("persona_id") or self.default_persona_id

        try:
            results = await engine.search_memories(
                query=query,
                k=limit,
                session_id=session_id,
                persona_id=persona_id,
            )

            memories = []
            for r in results:
                memories.append(
                    {
                        "id": getattr(r, "doc_id", None),
                        "content": getattr(r, "content", ""),
                        "score": getattr(r, "final_score", 0.0),
                        "metadata": getattr(r, "metadata", {}) or {},
                    }
                )

            return web.json_response(
                {
                    "ok": True,
                    "query": query,
                    "count": len(memories),
                    "memories": memories,
                }
            )
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] 检索失败: {e}", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_add(self, request: web.Request) -> web.Response:
        """POST /api/memory/add - 添加记忆记录"""
        await self._ensure_auto()
        engine = self._get_memory_engine()
        if not engine:
            return web.json_response(
                {"ok": False, "error": "LivingMemory 引擎尚未就绪或不可用"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的 JSON 请求体"}, status=400
            )

        content = data.get("content", "").strip()
        if not content:
            return web.json_response(
                {"ok": False, "error": "content 不能为空"}, status=400
            )

        importance = float(data.get("importance", 0.5))
        session_id = data.get("session_id") or self.default_session_id
        persona_id = data.get("persona_id") or self.default_persona_id
        metadata = dict(data.get("metadata") or {})
        if "source" not in metadata:
            metadata["source"] = "coopanion_pet"
        if "adapter_id" not in metadata:
            metadata["adapter_id"] = self.default_adapter_id
        metadata = self._enrich_metadata(
            content,
            session_id,
            persona_id,
            metadata,
            topics=data.get("topics"),
            key_facts=data.get("key_facts"),
            participants=data.get("participants"),
        )

        try:
            memory_id = await engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
            )
            return web.json_response({"ok": True, "memory_id": memory_id})
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] 添加记忆失败: {e}", exc_info=True)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_batch_add(self, request: web.Request) -> web.Response:
        """POST /api/memory/batch_add - 批量添加记忆（用于初始化迁移与批量同步）"""
        await self._ensure_auto()
        engine = self._get_memory_engine()
        if not engine:
            return web.json_response(
                {"ok": False, "error": "LivingMemory 引擎尚未就绪或不可用"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的 JSON 请求体"}, status=400
            )

        items = data.get("memories", [])
        if not isinstance(items, list) or not items:
            return web.json_response(
                {"ok": False, "error": "memories 必须为非空列表"}, status=400
            )

        added_ids = []
        errors = []

        for idx, item in enumerate(items):
            content = item.get("content", "").strip()
            if not content:
                continue
            importance = float(item.get("importance", 0.5))
            session_id = item.get("session_id") or self.default_session_id
            persona_id = item.get("persona_id") or self.default_persona_id
            metadata = dict(item.get("metadata") or {})
            if "source" not in metadata:
                metadata["source"] = "coopanion_migration"
            if "adapter_id" not in metadata:
                metadata["adapter_id"] = self.default_adapter_id
            metadata = self._enrich_metadata(
                content,
                session_id,
                persona_id,
                metadata,
                topics=item.get("topics"),
                key_facts=item.get("key_facts"),
                participants=item.get("participants"),
            )

            try:
                mid = await engine.add_memory(
                    content=content,
                    session_id=session_id,
                    persona_id=persona_id,
                    importance=importance,
                    metadata=metadata,
                )
                added_ids.append(mid)
            except Exception as e:
                errors.append({"index": idx, "error": str(e)})

        return web.json_response(
            {
                "ok": True,
                "total": len(items),
                "added_count": len(added_ids),
                "added_ids": added_ids,
                "errors": errors,
            }
        )

    async def handle_reflect(self, request: web.Request) -> web.Response:
        """POST /api/memory/reflect - 对桌宠会话做一次记忆蒸馏判定（达标才总结）"""
        await self._ensure_auto()
        try:
            data = await request.json()
        except Exception:
            data = {}
        session_id = (data.get("session_id") or self.default_session_id).strip()
        result = await self._run_reflection(session_id)
        return web.json_response(result, status=200 if result.get("ok") else 503)

    async def handle_chat(self, request: web.Request) -> web.Response:
        """POST /api/memory/chat - 接收桌宠端对话流注入会话窗口"""
        await self._ensure_auto()
        conv_mgr = self._get_conversation_manager()
        if not conv_mgr:
            return web.json_response(
                {"ok": False, "error": "ConversationManager 尚未就绪"}, status=503
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的 JSON 请求体"}, status=400
            )

        turns = data.get("turns", [])
        if not isinstance(turns, list) or not turns:
            return web.json_response(
                {"ok": False, "error": "turns 必须为非空列表"}, status=400
            )

        session_id = data.get("session_id") or self.default_session_id
        recorded = 0

        for turn in turns:
            role = turn.get("role")
            content = turn.get("content", "").strip()
            if not role or not content:
                continue

            ts = turn.get("timestamp") or time.time()
            is_bot = (role == "assistant")
            # 两侧身份必须与历史数据一致：bot = 账号号 + 人名，用户 = 账号号 + 映射人名。
            # 总结提示词要求消息前缀带具体昵称（禁止"用户/某人"），这里的 sender_name 会被直接拼进
            # [昵称 | ID: xxx | 时间] 前缀，写错就会让人物识别错位。
            parts = [p for p in str(session_id).split(":") if p]
            user_id = next(
                (p for p in parts[1:] if p.isdigit() and p != self.bot_sender_id), ""
            )
            if is_bot:
                sender_id = self.bot_sender_id
                sender_name = self.user_display_names.get(
                    self.bot_sender_id, self.bot_display_name
                )
            else:
                sender_id = user_id or "user"
                sender_name = self.user_display_names.get(sender_id, sender_id)

            try:
                await conv_mgr.add_message(
                    session_id=session_id,
                    role=role,
                    content=content,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    platform="aiocqhttp",
                    is_bot_message=is_bot,
                    metadata={"source": "coopanion_desktop_pet"},
                    timestamp=ts,
                )
                recorded += 1
            except Exception as e:
                logger.warning(f"[LivingMemoryBridge] 记录对话单条失败: {e}")

        # 桌宠对话写入后按 LivingMemory 的规则判定是否蒸馏（达标才排后台总结任务）
        reflection: dict[str, Any] | None = None
        if self.enable_auto_reflection and recorded > 0:
            try:
                reflection = await self._run_reflection(session_id)
            except Exception as e:
                logger.warning(f"[LivingMemoryBridge] 自动蒸馏判定失败: {e}")

        return web.json_response(
            {"ok": True, "recorded": recorded, **({"reflection": reflection} if reflection else {})}
        )

    async def start(self):
        """启动 HTTP 桥服务"""
        try:
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
            logger.info(
                f"[LivingMemoryBridge] HTTP 桥服务已成功启动: http://{self.host}:{self.port}"
            )
        except Exception as e:
            logger.error(
                f"[LivingMemoryBridge] 启动 HTTP 桥服务失败 (http://{self.host}:{self.port}): {e}",
                exc_info=True,
            )

    async def stop(self):
        """停止 HTTP 桥服务"""
        if self._runner:
            try:
                await self._runner.cleanup()
                logger.info("[LivingMemoryBridge] HTTP 桥服务已停止")
            except Exception as e:
                logger.error(f"[LivingMemoryBridge] 停止服务异常: {e}")
            finally:
                self._runner = None
                self._site = None
