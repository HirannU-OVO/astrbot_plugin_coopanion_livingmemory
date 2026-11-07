"""bridge_server.py - LivingMemory HTTP Bridge Server
提供本地 REST API 端点供 Coopanion 桌宠扩展进行高速直连交互
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
from typing import Any

from aiohttp import web
from astrbot.api import logger
from astrbot.api.star import Context

# 身份键统一为 "<平台>:<会话/用户ID>"。平台名来自消息自带的适配器/平台字段，
# 不再设白名单；没有任何平台标识时归入固定的本地命名空间。
LOCAL_PLATFORM = "local"
SUPPORTED_PLATFORMS = frozenset({"aiocqhttp", LOCAL_PLATFORM})
LOCAL_PLATFORM_PREFIXES = frozenset(
    {"coopanion", "desktop-pet", "desktop_pet", "local", "import", "manual"}
)
KNOWN_PLATFORM_NAMES = frozenset(
    {
        "aiocqhttp",
        "telegram",
        "webchat",
        "feishu",
        "lark",
        "wecom",
        "wechat",
        "qq",
        "discord",
        "slack",
        "dingtalk",
        "matrix",
    }
)


VERSION = "0.2.0"


class LivingMemoryBridgeServer:
    # 自动识别可覆盖的"历史占位值"：这些值和空值一样视为尚未人工确认。
    _AUTO_SENTINELS = {
        "default_session_id": {"", "coopanion:default"},
        "default_persona_id": {"", "default"},
    }

    def __init__(self, context: Context, config: dict[str, Any]):
        self.context = context
        self.config = config
        self.host = config.get("host", "127.0.0.1")
        self.port = int(config.get("port", 6198))
        # 开源默认值不带任何真实账号：留空时先自动检测，检测不到才用占位
        self.default_session_id = str(
            config.get("default_session_id") or ""
        ).strip() or "coopanion:default"
        self.default_persona_id = "default"
        # 人格：留空或历史默认值 "default" 视为"自动解析"；由 _ensure_auto 从
        # LivingMemory 现有记忆 / AstrBot 人格配置里解析出真实人格标签。
        self._configured_persona = str(config.get("default_persona_id") or "").strip()
        self._persona_is_auto = (
            not self._configured_persona or self._configured_persona == "default"
        )
        if self._configured_persona:
            self.default_persona_id = self._configured_persona
        self.default_adapter_id = str(config.get("default_adapter_id") or "").strip()
        self.usage_mode = str(config.get("usage_mode") or "qq").strip().lower()
        if self.usage_mode not in ("qq", "local"):
            self.usage_mode = "qq"
        if self.usage_mode == "local":
            self.default_adapter_id = ""
        # 记忆共享模式：shared=一起记；pet_only=桌宠单独记
        self.memory_share_mode = str(
            config.get("memory_share_mode") or "shared"
        ).strip().lower()
        if self.memory_share_mode not in ("shared", "pet_only"):
            self.memory_share_mode = "shared"
        if self.usage_mode == "local":
            self.memory_share_mode = "shared"
        self.pet_persona_id = str(config.get("pet_persona_id") or "").strip()
        self.read_scope_sessions_text = str(
            config.get("read_scope_sessions_text") or ""
        )
        self._mode_state: dict[str, Any] = {}
        self._persona_filtering_warning = ""
        self._persona_filtering_logged = False
        # 参与者显示名/别名：优先级 user_map > 旧文本 > 内部 dict 配置。
        internal_names = {
            str(k).strip(): str(v).strip()
            for k, v in dict(config.get("user_display_names") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        internal_aliases = {
            str(k).strip(): [
                str(a).strip() for a in (v or []) if str(a).strip()
            ]
            for k, v in dict(config.get("user_aliases") or {}).items()
            if str(k).strip()
        }
        old_names = self._parse_kv(str(config.get("user_names_text") or ""))
        old_aliases = self._parse_alias_text(
            str(config.get("user_aliases_text") or "")
        )
        raw_user_map = config.get("user_map")
        self.user_map = (
            {}
            if self.usage_mode == "local"
            else self._parse_user_map(raw_user_map)
        )
        if (
            self.usage_mode != "local"
            and not raw_user_map
            and not self.user_map
            and (old_names or old_aliases)
        ):
            generated: dict[str, str] = {}
            migrated_entries: list[str] = []
            for uid in list(old_names) + [u for u in old_aliases if u not in old_names]:
                parts = [old_names.get(uid, "")]
                parts.extend(old_aliases.get(uid) or [])
                parts = [p for p in parts if p]
                if not parts:
                    continue
                generated[uid] = "/".join(parts)
                migrated_entries.append(f"{uid}={','.join(parts)}")
            self.user_map = generated
            if migrated_entries:
                self._persist_migrated_user_map(migrated_entries)
        self.user_display_names = dict(internal_names)
        self.user_display_names.update(old_names)
        self.user_alias_cfg = {uid: list(v) for uid, v in internal_aliases.items()}
        self.user_alias_cfg.update(
            {uid: list(v) for uid, v in old_aliases.items()}
        )
        for uid, raw_value in self.user_map.items():
            parts = [p.strip() for p in re.split(r"[,，/|、]", str(raw_value)) if p.strip()]
            if not parts:
                continue
            self.user_display_names[uid] = parts[0]
            if len(parts) > 1:
                self.user_alias_cfg[uid] = parts[1:]
            else:
                self.user_alias_cfg.pop(uid, None)
        if self.usage_mode == "local":
            # 纯桌宠不解析平台映射，但配置文件里的原值保留
            self.user_map = {}
            self.user_display_names = {}
            self.user_alias_cfg = {}
        # 桌宠（bot）的身份：id 用账号号、name 用称呼，其余叫法放 aliases。
        # 名字变体不登记为别名，LivingMemory 会把同一个人拆成两个人物节点。
        bot_cfg = config.get("bot_identity") or {}
        # 面板只让填纯文本（不填 JSON）：bot_qq / bot_name / user_name
        self._cfg_bot_qq = str(config.get("bot_qq") or "").strip()
        self._cfg_bot_name = str(config.get("bot_name") or "").strip()
        self._cfg_user_name = str(config.get("user_name") or "").strip()
        # 未配置时留空，等 _ensure_auto 从历史数据解析；绝不再拿 persona 名当
        # bot 账号，否则会在图谱里造出 person:account:default 这种假人节点。
        self.bot_sender_id = self._cfg_bot_qq or str(bot_cfg.get("id") or "").strip()
        self.bot_display_name = self._cfg_bot_name or str(bot_cfg.get("name") or "").strip()
        if self.usage_mode == "local":
            self.bot_sender_id = ""
        self.bot_aliases: list[str] = [
            str(a).strip()
            for a in (bot_cfg.get("aliases") or [])
            if str(a).strip() and str(a).strip() != self.bot_display_name
        ]
        raw_bot_aliases = config.get("bot_aliases_text")
        for alias in self._parse_alias_list(raw_bot_aliases):
            if alias != self.bot_display_name and alias not in self.bot_aliases:
                self.bot_aliases.append(alias)
        if self.usage_mode == "local":
            self.bot_aliases = []
        if isinstance(raw_bot_aliases, str) and raw_bot_aliases.strip():
            migrated_aliases = self._parse_alias_list(raw_bot_aliases)
            if migrated_aliases:
                self._persist_migrated_bot_aliases(migrated_aliases)
        # 自动检测：失败不算失败，保持 pending，下个请求继续尝试（避免引擎未就绪时永久降级）
        self._auto_ready = False
        self._auto_warned = False
        self._identity_warning = ""
        self._persona_warning = ""
        self._bot_session_cache: dict[str, tuple[str, str]] = {}
        self._warned_platforms: set[str] = set()
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
            expected = f"Bearer {self.auth_token}"
            if not hmac.compare_digest(got, expected):
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
        if self.usage_mode == "local":
            # 纯桌宠模式不探测平台/会话/身份
            self._auto_ready = True
            return
        pending = False
        try:
            if self.default_session_id == "coopanion:default":
                sid = await self._detect_session()
                if sid:
                    self.default_session_id = sid
                    logger.info(f"[LivingMemoryBridge] 自动检测会话: {sid}")
                else:
                    pending = True
        except Exception as e:
            pending = True
            logger.debug(f"[LivingMemoryBridge] 会话自动检测失败: {e}")

        uid = self._session_user_id()
        raw_uid = self._session_user_id_for(self.default_session_id, "")

        # bot_qq 与用户号相同属于自指配置：忽略配置值并回退自动识别
        if self.bot_sender_id and raw_uid and self.bot_sender_id == raw_uid:
            self._identity_warning = (
                f"bot_qq({self.bot_sender_id}) 与用户 QQ 相同，已忽略该身份配置；"
                "请在面板改成桌宠真实 QQ 号"
            )
            logger.error(f"[LivingMemoryBridge] {self._identity_warning}")
            self._cfg_bot_qq = ""
            self.bot_sender_id = ""
            self._bot_session_cache.clear()

        if self._persona_is_auto:
            try:
                pid = await self._detect_persona(self.default_session_id)
                if pid:
                    self.default_persona_id = pid
                    logger.info(f"[LivingMemoryBridge] 自动检测人格: {pid}")
                    if not await self._persona_exists(pid):
                        self._persona_warning = (
                            f"人格 {pid} 不在 AstrBot 人格列表中（可能已改名）；"
                            f"旧记忆仍用它，可用 /lmbridge migrate-persona {pid} <新人格> 迁移"
                        )
                        logger.warning(f"[LivingMemoryBridge] {self._persona_warning}")
                else:
                    pending = True
            except Exception as e:
                pending = True
                logger.debug(f"[LivingMemoryBridge] 人格自动检测失败: {e}")
        elif self._configured_persona:
            try:
                if not await self._persona_exists(self._configured_persona):
                    fallback = await self._detect_persona(self.default_session_id)
                    self._persona_warning = (
                        f"配置的人格 {self._configured_persona} 不存在，已回退到"
                        f"{fallback or 'AstrBot 默认人格'}"
                    )
                    logger.warning(f"[LivingMemoryBridge] {self._persona_warning}")
                    if fallback:
                        self.default_persona_id = fallback
                    else:
                        pending = True
            except Exception as e:
                pending = True
                logger.debug(f"[LivingMemoryBridge] 人格校验失败: {e}")

        try:
            if not (self._cfg_bot_qq and self._cfg_bot_name):
                found = self._detect_identities()
                if found:
                    bot_id, bot_name, user_id, user_name = found
                    if not self._cfg_bot_qq and bot_id:
                        self.bot_sender_id = bot_id
                    if not self._cfg_bot_name and bot_name:
                        self.bot_display_name = bot_name
                    if user_id and user_name:
                        self.user_display_names.setdefault(user_id, user_name)
                    logger.info(
                        f"[LivingMemoryBridge] 自动检测身份: bot={bot_id}/{bot_name} user={user_id}/{user_name}"
                    )
                    if not (bot_id and user_id):
                        pending = True
                else:
                    pending = True
        except Exception as e:
            pending = True
            logger.debug(f"[LivingMemoryBridge] 身份自动检测失败: {e}")

        self._auto_ready = not pending
        # 无论是否完全识别，先把已知字段写回面板（bot 未识别不阻塞）
        await self._persist_detected_config(self._collect_detected_config())
        self._record_mode_state()
        if self.memory_share_mode == "pet_only" and not self._persona_filtering_logged:
            self._persona_filtering_logged = True
            if not self._persona_filtering_enabled():
                self._persona_filtering_warning = (
                    "上游「按人格过滤」未开启，桌宠单独记不会生效"
                )
                logger.warning(f"[LivingMemoryBridge] {self._persona_filtering_warning}")
        if pending and not self._auto_warned:
            self._auto_warned = True
            logger.warning(
                "[LivingMemoryBridge] 自动检测未完成（会话/人格/身份），后续请求会重试；"
                "可在面板手动填写身份、人格或会话配置"
            )

    def _normalize_platform_name(self, platform: Any) -> str:
        raw = str(platform or "").strip().casefold()
        if not raw:
            return LOCAL_PLATFORM
        raw = raw.split(":", 1)[0].strip()
        if raw in LOCAL_PLATFORM_PREFIXES:
            return LOCAL_PLATFORM
        raw = re.sub(r"[^a-z0-9_.-]+", "_", raw).strip("_.-")
        return raw or LOCAL_PLATFORM

    def _is_supported_platform(self, platform: Any) -> bool:
        return self._normalize_platform_name(platform) in SUPPORTED_PLATFORMS

    def _session_platform(
        self, session_id: str, explicit_platform: Any = None
    ) -> str:
        """解析平台名：消息自带字段优先，其次 adapter 实例映射，最后本地命名空间。"""
        if self.usage_mode == "local":
            return LOCAL_PLATFORM
        if str(explicit_platform or "").strip():
            return self._normalize_platform_name(explicit_platform)
        parts = [p for p in str(session_id or "").split(":") if p]
        if not parts:
            return LOCAL_PLATFORM
        prefix = parts[0]
        if prefix.casefold() in LOCAL_PLATFORM_PREFIXES:
            return LOCAL_PLATFORM
        if prefix.casefold() in KNOWN_PLATFORM_NAMES:
            return self._normalize_platform_name(prefix)
        try:
            for inst in self.context.platform_manager.get_insts():
                try:
                    meta = inst.meta()
                except Exception:
                    continue
                if str(getattr(meta, "id", "") or "").strip() == prefix:
                    name = str(getattr(meta, "name", "") or "").strip()
                    if name:
                        return self._normalize_platform_name(name)
                    break
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 解析会话平台失败: {e}")
        # 认不出平台来源（未知 adapter 实例、桌宠本地通道）统一进本地命名空间
        return LOCAL_PLATFORM

    def _session_adapter_id(self, session_id: str | None = None) -> str:
        """当前会话的适配器实例名（umo 第一段）；桌宠自建会话回退配置值。"""
        if self.usage_mode == "local":
            return ""
        sid = str(session_id or self.default_session_id or "").strip()
        parts = [p for p in sid.split(":") if p]
        if parts and parts[0] != "coopanion":
            return parts[0]
        return self.default_adapter_id

    _PLATFORM_DISPLAY = {
        "aiocqhttp": "QQ",
        "telegram": "Telegram",
        "webchat": "WebChat",
        "feishu": "飞书",
        "lark": "飞书",
        "wecom": "企业微信",
        "wechat": "微信",
        "qq": "QQ",
        "discord": "Discord",
        "slack": "Slack",
        "dingtalk": "钉钉",
        "local": "本地",
    }

    def _platform_label(self, platform: str | None) -> str:
        """平台类型的中文说明，例如 aiocqhttp（QQ）。"""
        code = str(platform or "").strip()
        if not code:
            return "未识别"
        display = self._platform_display_name(code)
        return f"{code}（{display}）" if display != code else code

    def _platform_display_name(self, platform: str | None) -> str:
        code = str(platform or "").strip()
        if not code:
            return "未知"
        return self._PLATFORM_DISPLAY.get(code.casefold(), code)

    def _read_scope_sessions(self, session_id: str) -> list[str]:
        """私聊/桌宠读取时额外纳入的群会话；群会话本身只读自己。"""
        sid = str(session_id or "").strip()
        if self.usage_mode == "local":
            return [sid] if sid else []
        parts = [p for p in sid.split(":") if p]
        is_group = len(parts) > 1 and "group" in parts[1].casefold()
        if is_group:
            return [sid] if sid else []
        extra: list[str] = []
        for raw in re.split(r"[,，;；\n]", self.read_scope_sessions_text or ""):
            item = raw.strip()
            if not item or item == sid or item in extra:
                continue
            if not self._is_supported_platform(self._session_platform(item)):
                logger.warning(
                    f"[LivingMemoryBridge] 读取范围里的会话 {item} 平台暂未支持，已忽略"
                )
                continue
            extra.append(item)
        return ([sid] if sid else []) + extra

    async def _search_with_read_scope(
        self,
        engine: Any,
        query: str,
        limit: int,
        session_id: str,
        persona_id: str,
    ) -> list[Any]:
        sessions = self._read_scope_sessions(session_id)
        if not sessions:
            return []
        merged: dict[Any, Any] = {}
        for sid in sessions:
            try:
                rows = await engine.search_memories(
                    query=query,
                    k=limit,
                    session_id=sid,
                    persona_id=persona_id,
                )
            except Exception as e:
                if sid != session_id:
                    logger.warning(
                        f"[LivingMemoryBridge] 读取群会话 {sid} 失败，已跳过: {e}"
                    )
                    continue
                raise
            for row in rows:
                key = getattr(row, "doc_id", None)
                if key is None:
                    key = id(row)
                old = merged.get(key)
                if old is None or getattr(row, "final_score", 0.0) > getattr(
                    old, "final_score", 0.0
                ):
                    merged[key] = row
        out = list(merged.values())
        out.sort(key=lambda row: float(getattr(row, "final_score", 0.0)), reverse=True)
        return out[:limit]

    def _effective_pet_persona(self) -> str:
        if self.pet_persona_id:
            return self.pet_persona_id
        base = self.default_persona_id
        if not base or base == "default":
            base = "默认"
        return f"{base}-桌宠"

    def _effective_persona(self, requested: Any = None) -> str:
        if self.usage_mode == "local":
            return str(requested or "").strip() or self.default_persona_id
        if self.memory_share_mode == "pet_only":
            return self._effective_pet_persona()
        return str(requested or "").strip() or self.default_persona_id

    def _chat_storage_session(self, session_id: str) -> str:
        """桌宠单独记时，把桌宠对话挪到独立会话，避免混进 QQ 的总结窗口。"""
        if self.usage_mode == "local" or self.memory_share_mode != "pet_only":
            return str(session_id or "").strip()
        sid = str(session_id or "").strip()
        if not sid:
            return "local:pet#pet"
        if sid.endswith("#pet"):
            return sid
        return f"{sid}#pet"

    def _fallback_sender_name(self, is_bot: bool) -> str:
        """纯桌宠没有账号时，优先用面板里的称呼，不用写死「桌宠 / 我」。"""
        if self.usage_mode == "local":
            return "桌宠" if is_bot else (self._cfg_user_name or "我")
        if is_bot:
            return self.bot_display_name or self._cfg_bot_name or "桌宠"
        return "我"

    def _persona_filtering_enabled(self) -> bool:
        raw = self._livingmemory_config_get(
            "filtering_settings.use_persona_filtering", True
        )
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("false", "0", "off", "no")

    def _mode_state_path(self) -> str | None:
        try:
            from astrbot.api.star import StarTools

            data_dir = str(StarTools.get_data_dir("astrbot_plugin_coopanion_livingmemory"))
            return os.path.join(data_dir, "memory_share_state.json")
        except Exception:
            return None

    def _load_mode_state(self) -> dict[str, Any]:
        path = self._mode_state_path()
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _record_mode_state(self) -> None:
        path = self._mode_state_path()
        if not path:
            return
        old = self._load_mode_state()
        current = {
            "mode": self.memory_share_mode,
            "default_persona": self.default_persona_id,
            "pet_persona": self._effective_pet_persona(),
        }
        self._mode_state = old
        if not old:
            current["switched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(current, f, ensure_ascii=False, indent=2)
                self._mode_state = current
            except Exception as e:
                logger.debug(f"[LivingMemoryBridge] 写入记忆模式状态失败: {e}")
            return
        old_key = (
            str(old.get("mode") or ""),
            str(old.get("default_persona") or ""),
            str(old.get("pet_persona") or ""),
        )
        new_key = (
            current["mode"],
            current["default_persona"],
            current["pet_persona"],
        )
        if old_key == new_key:
            return
        state = {
            **current,
            "switched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "old_mode": old_key[0],
            "old_persona": old_key[2]
            if old_key[0] == "pet_only"
            else old_key[1],
            "new_persona": current["pet_persona"]
            if current["mode"] == "pet_only"
            else current["default_persona"],
        }
        self._mode_state = state
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logger.info(
                f"[LivingMemoryBridge] 记忆共享模式切换: {state['old_mode']} -> {state['mode']}，"
                f"旧人格 {state.get('old_persona')}，新人格 {state.get('new_persona')}"
            )
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 写入记忆模式状态失败: {e}")

    def _conversation_counts(self) -> dict[str, int]:
        """conversations.db 里每个会话的消息条数。"""
        import sqlite3

        from astrbot.api.star import StarTools

        db = os.path.join(
            str(StarTools.get_data_dir("astrbot_plugin_livingmemory")),
            "conversations.db",
        )
        out: dict[str, int] = {}
        if not os.path.exists(db):
            return out
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                rows = con.execute(
                    "SELECT session_id, COUNT(*) FROM messages "
                    "WHERE session_id IS NOT NULL AND session_id <> '' "
                    "GROUP BY session_id"
                ).fetchall()
            finally:
                con.close()
            for session_id, count in rows:
                out[str(session_id)] = int(count or 0)
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取会话消息条数失败: {e}")
        return out

    def _session_personas(self) -> dict[str, str]:
        """每个会话在 documents 里出现最多的人格标签。"""
        import sqlite3

        from astrbot.api.star import StarTools

        db = os.path.join(
            str(StarTools.get_data_dir("astrbot_plugin_livingmemory")),
            "livingmemory.db",
        )
        out: dict[str, str] = {}
        if not os.path.exists(db):
            return out
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                rows = con.execute(
                    "SELECT json_extract(metadata,'$.session_id') AS sid, "
                    "json_extract(metadata,'$.persona_id') AS p, COUNT(*) AS c "
                    "FROM documents "
                    "WHERE json_extract(metadata,'$.session_id') IS NOT NULL "
                    "AND json_extract(metadata,'$.session_id') <> '' "
                    "AND json_extract(metadata,'$.persona_id') IS NOT NULL "
                    "AND json_extract(metadata,'$.persona_id') <> '' "
                    "GROUP BY sid, p ORDER BY c DESC"
                ).fetchall()
            finally:
                con.close()
            for sid, persona, _count in rows:
                out.setdefault(str(sid), str(persona))
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取会话人格失败: {e}")
        return out

    @staticmethod
    def _metric_count(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, dict):
            return int(value.get("count") or value.get("total") or 0)
        return 0

    async def get_session_overview(self) -> dict[str, Any]:
        """汇总检测到的适配器与会话，供面板下拉和 /lmbridge 共用。"""
        stats_sessions: dict[str, Any] = {}
        try:
            engine = self._get_memory_engine()
            if engine is not None and hasattr(engine, "get_statistics"):
                stats = await engine.get_statistics()
                stats_sessions = (stats or {}).get("sessions") or {}
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取会话统计失败: {e}")

        conv_counts = self._conversation_counts()
        personas = self._session_personas()
        session_ids = set(conv_counts) | set(stats_sessions)
        rows: list[dict[str, Any]] = []
        for raw_sid in session_ids:
            sid = str(raw_sid or "").strip()
            if not sid or sid == "coopanion:default":
                continue
            platform = self._session_platform(sid)
            if platform not in SUPPORTED_PLATFORMS:
                continue
            platform_code = platform
            adapter_id = self._session_adapter_id(sid)
            count = conv_counts.get(sid)
            if count is None:
                count = self._metric_count(stats_sessions.get(sid))
            rows.append(
                {
                    "session_id": sid,
                    "adapter_id": adapter_id,
                    "platform": platform_code,
                    "platform_name": self._platform_display_name(platform_code),
                    "platform_label": f"{self._platform_display_name(platform_code)} / {platform_code}",
                    "persona_id": personas.get(sid, ""),
                    "message_count": count,
                    "memory_count": self._metric_count(stats_sessions.get(sid)),
                }
            )
        rows.sort(key=lambda item: (item["message_count"], item["session_id"]), reverse=True)

        adapter_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            aid = row["adapter_id"]
            if not aid:
                continue
            item = adapter_map.setdefault(
                aid,
                {
                    "id": aid,
                    "platform": row["platform"],
                    "platform_name": row["platform_name"],
                    "message_count": 0,
                    "session_count": 0,
                },
            )
            item["message_count"] += row["message_count"]
            item["session_count"] += 1

        adapters = sorted(
            adapter_map.values(),
            key=lambda item: (item["message_count"], item["id"]),
            reverse=True,
        )
        for adapter in adapters:
            code = adapter["platform"]
            display = self._platform_display_name(code)
            adapter["label"] = (
                f"{adapter['id']}（{display} / {code}）"
                if code and code != "unknown"
                else f"{adapter['id']}（未知平台）"
            )
        for row in rows:
            code = row["platform"]
            display = row["platform_name"]
            row["label"] = (
                f"{row['session_id']}（{display} / {code} / {row['message_count']} 条）"
            )
        return {"adapters": adapters, "sessions": rows}

    def _bot_identity_for_session(self, session_id: str) -> tuple[str, str]:
        """按会话取 bot 身份：优先该会话 assistant 消息的 sender，其次全局配置。"""
        if self.usage_mode == "local":
            return ("", "桌宠")
        cached = self._bot_session_cache.get(session_id)
        if cached:
            return cached
        result = (self.bot_sender_id, self.bot_display_name)
        try:
            import os
            import sqlite3

            from astrbot.api.star import StarTools

            data_dir = str(StarTools.get_data_dir("astrbot_plugin_livingmemory"))
            db = os.path.join(data_dir, "conversations.db")
            if os.path.exists(db):
                con = None
                try:
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
                    row = con.execute(
                        "SELECT sender_id, sender_name FROM messages "
                        "WHERE session_id=? AND sender_id IS NOT NULL AND sender_id<>'' "
                        "AND (role='assistant' "
                        "OR COALESCE(json_extract(metadata,'$.is_bot_message'),0)=1) "
                        "ORDER BY id DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                finally:
                    if con is not None:
                        con.close()
                if row:
                    bid = str(row[0] or "").strip()
                    bname = str(row[1] or "").strip() or bid
                    if bid:
                        result = (bid, bname)
                        self._bot_session_cache[session_id] = result
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 会话 bot 身份查询失败: {e}")
        return result

    @staticmethod
    def _session_user_id_for(session_id: str, bot_id: str = "") -> str:
        """从会话 umo 的冒号段里取用户数字账号（排除 bot 账号）。"""
        parts = [p for p in str(session_id or "").split(":") if p]
        return next((p for p in parts[1:] if p.isdigit() and p != bot_id), "")

    async def _persona_exists(self, persona_id: str) -> bool:
        name = str(persona_id or "").strip()
        if not name:
            return False
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return True
        try:
            personas = await manager.get_all_personas()
            if isinstance(personas, (list, tuple)):
                return any(
                    str(getattr(persona, "persona_id", "")).strip() == name
                    for persona in personas
                )
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取人格列表失败: {e}")
        try:
            await manager.get_persona(name)
            return True
        except Exception:
            return False

    def _collect_detected_config(self) -> dict[str, Any]:
        """把本次自动识别结果整理成可写回插件配置的字段。"""
        uid = self._session_user_id()
        session_id = str(self.default_session_id or "").strip()
        detected: dict[str, Any] = {}
        if session_id and session_id != "coopanion:default":
            detected["default_session_id"] = session_id
        if self.default_persona_id and self.default_persona_id != "default":
            detected["default_persona_id"] = self.default_persona_id
        if self.bot_sender_id.isdigit():
            detected["bot_qq"] = self.bot_sender_id
        if self.bot_display_name and self.bot_display_name != self.bot_sender_id:
            detected["bot_name"] = self.bot_display_name
        bot_aliases = [
            alias
            for alias in self.bot_aliases
            if alias and alias != self.bot_display_name
        ]
        if bot_aliases:
            detected["bot_aliases_text"] = list(bot_aliases)
        user_name = self.user_display_names.get(uid, "") if uid else ""
        if uid and user_name and not user_name.isdigit():
            detected["user_name"] = user_name
            detected["user_names_text"] = f"{uid}={user_name}"
        user_aliases = self.user_alias_cfg.get(uid) if uid else None
        if uid and user_aliases:
            detected["user_aliases_text"] = f"{uid}={'/'.join(user_aliases)}"
        return detected

    def _auto_state_path(self) -> str | None:
        try:
            from astrbot.api.star import StarTools

            data_dir = str(
                StarTools.get_data_dir("astrbot_plugin_coopanion_livingmemory")
            )
            os.makedirs(data_dir, exist_ok=True)
            return os.path.join(data_dir, "auto_identity_state.json")
        except Exception:
            return None

    def _load_auto_state(self) -> dict[str, Any]:
        path = self._auto_state_path()
        if not path or not os.path.exists(path):
            return {"written": {}}
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and isinstance(state.get("written"), dict):
                return state
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取自动识别状态失败: {e}")
        return {"written": {}}

    def _save_auto_state(self, state: dict[str, Any]) -> None:
        path = self._auto_state_path()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 写入自动识别状态失败: {e}")

    async def _persist_detected_config(self, detected: dict[str, Any]) -> None:
        """把识别结果写回插件配置；用户手动改过的项保留用户值。"""
        if self.usage_mode == "local" or not detected:
            return
        state = self._load_auto_state()
        written = state.setdefault("written", {})
        patch: dict[str, Any] = {}
        for key, value in detected.items():
            if key == "bot_aliases_text":
                aliases = self._parse_alias_list(value)
                if not aliases:
                    continue
                current_aliases = self._parse_alias_list(self.config.get(key))
                if current_aliases == aliases:
                    continue
                last_auto_aliases = self._parse_alias_list(written.get(key))
                auto_managed = (
                    not current_aliases or current_aliases == last_auto_aliases
                )
                if not auto_managed:
                    logger.info(
                        f"[LivingMemoryBridge] 配置项 {key} 已被手动修改"
                        f"（{current_aliases}），保留用户值，不自动覆盖"
                    )
                    continue
                patch[key] = aliases
                continue
            text = str(value or "").strip()
            if not text:
                continue
            current = str(self.config.get(key) or "").strip()
            if current == text:
                continue
            last_auto = str(written.get(key) or "").strip()
            sentinels = self._AUTO_SENTINELS.get(key, {""})
            auto_managed = (not current) or current in sentinels or current == last_auto
            if not auto_managed:
                logger.info(
                    f"[LivingMemoryBridge] 配置项 {key} 已被手动修改（{current}），"
                    "保留用户值，不自动覆盖"
                )
                continue
            patch[key] = text
        if not patch:
            return
        try:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                saved = await saver(patch)
                if saved is False:
                    raise RuntimeError("save_config_async 返回 False")
            else:
                saver = getattr(self.config, "save_config", None)
                if not callable(saver):
                    logger.warning(
                        "[LivingMemoryBridge] 当前配置对象不支持保存，识别结果仅在本次运行生效"
                    )
                    return
                saver(patch)
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 自动识别结果写入配置失败: {e}")
            return
        written.update(patch)
        self._save_auto_state(state)
        self._apply_detected_patch(patch)
        logger.info(
            "[LivingMemoryBridge] 已把自动识别结果写入插件配置（面板可见，可确认/修改）: "
            + ", ".join(f"{key}={value}" for key, value in patch.items())
        )

    def _apply_detected_patch(self, patch: dict[str, Any]) -> None:
        """写回配置后同步内存状态，避免 health 与面板/配置文件不一致。"""
        if "default_session_id" in patch:
            self.default_session_id = patch["default_session_id"]
        if "default_persona_id" in patch:
            self.default_persona_id = patch["default_persona_id"]
        if "default_adapter_id" in patch:
            self.default_adapter_id = patch["default_adapter_id"]
        if "bot_qq" in patch:
            self._cfg_bot_qq = patch["bot_qq"]
            self.bot_sender_id = patch["bot_qq"]
        if "bot_name" in patch:
            self._cfg_bot_name = patch["bot_name"]
            self.bot_display_name = patch["bot_name"]
        if "bot_aliases_text" in patch:
            self.bot_aliases = self._parse_alias_list(patch["bot_aliases_text"])
        if "user_name" in patch:
            self._cfg_user_name = patch["user_name"]
            uid = self._session_user_id()
            if uid:
                self.user_display_names[uid] = patch["user_name"]

    async def _detect_session(self) -> str | None:
        if self.usage_mode == "local":
            return None
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

    async def _detect_persona(self, session_id: str) -> str | None:
        """解析当前会话的真实人格：优先现有记忆的主导人格，其次 AstrBot 默认人格。"""
        import os
        import sqlite3

        from astrbot.api.star import StarTools

        db = os.path.join(
            str(StarTools.get_data_dir("astrbot_plugin_livingmemory")),
            "livingmemory.db",
        )
        if os.path.exists(db):
            con = None
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
                row = con.execute(
                    "SELECT json_extract(metadata,'$.persona_id') AS p, COUNT(*) AS c "
                    "FROM documents WHERE json_extract(metadata,'$.session_id')=? "
                    "AND p IS NOT NULL AND p <> '' GROUP BY p ORDER BY c DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            finally:
                if con is not None:
                    con.close()
            if row and row[0]:
                return str(row[0])
        try:
            default = await self.context.persona_manager.get_default_persona_v3(
                umo=session_id
            )
            if default and default.get("name"):
                return str(default["name"])
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取 AstrBot 默认人格失败: {e}")
        return None

    def _detect_identities(self) -> tuple[str, str, str, str] | None:
        """从会话历史与现有记忆里读出 (bot_id, bot_name, user_id, user_name)。只读。

        先查 conversations.db（role / metadata.is_bot_message 判定），跳过桌宠桥自己
        写入的消息（source=coopanion_desktop_pet），避免自证；再回退到 LivingMemory
        已有记忆的 participant_identities。
        """
        import os
        import sqlite3

        from astrbot.api.star import StarTools

        data_dir = str(StarTools.get_data_dir("astrbot_plugin_livingmemory"))
        bot = user = None

        db = os.path.join(data_dir, "conversations.db")
        if os.path.exists(db):
            con = None
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
                rows = con.execute(
                    "SELECT sender_id, sender_name, role, "
                    "COALESCE(json_extract(metadata,'$.is_bot_message'),0), "
                    "COALESCE(json_extract(metadata,'$.source'),'') "
                    "FROM messages WHERE sender_id IS NOT NULL AND sender_id <> '' "
                    "ORDER BY id DESC LIMIT 500"
                ).fetchall()
            finally:
                if con is not None:
                    con.close()
            for sid, name, role, is_bot, source in rows:
                if str(source or "").strip() == "coopanion_desktop_pet":
                    continue
                sid_s = str(sid or "").strip()
                if not sid_s.isdigit():
                    continue
                name_s = str(name or "").strip() or sid_s
                if role == "assistant" or is_bot:
                    bot = bot or (sid_s, name_s)
                else:
                    user = user or (sid_s, name_s)
                if bot and user:
                    break

        fallback_bot, fallback_user = self._detect_identities_from_documents(data_dir)
        if bot is None:
            bot = fallback_bot
        elif fallback_bot and (not bot[1] or bot[1].isdigit()):
            bot = (bot[0], fallback_bot[1])
        if user is None:
            user = fallback_user
        elif fallback_user and (not user[1] or user[1].isdigit()):
            user = (user[0], fallback_user[1])
        if bot is None and user is None:
            return None
        b = bot or ("", "")
        u = user or ("", "")
        return (b[0], b[1], u[0], u[1])

    def _detect_identities_from_documents(
        self, data_dir: str
    ) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
        """从 LivingMemory 现有记忆的 participant_identities 里反查双方身份。"""
        import os
        import sqlite3

        db = os.path.join(data_dir, "livingmemory.db")
        if not os.path.exists(db):
            return (None, None)
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            rows = con.execute(
                "SELECT json_extract(je.value,'$.identity_key'), "
                "json_extract(je.value,'$.display_name'), "
                "json_extract(je.value,'$.is_bot'), COUNT(*) AS c "
                "FROM documents d, json_each(d.metadata,'$.participant_identities') je "
                "GROUP BY 1,2,3 ORDER BY c DESC"
            ).fetchall()
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 读取记忆参与者失败: {e}")
            return (None, None)
        finally:
            if con is not None:
                con.close()

        user_id = self._session_user_id()
        bot = user = None
        for key, name, is_bot, _count in rows:
            key_s = str(key or "").strip()
            if ":" not in key_s:
                continue
            platform_part, _, id_part = key_s.partition(":")
            if not platform_part or not id_part:
                continue
            name_s = str(name or "").strip()
            if is_bot and bot is None:
                bot = (id_part, name_s or id_part)
            elif not is_bot and user is None:
                if not user_id or id_part == user_id:
                    if name_s and not name_s.isdigit():
                        user = (id_part, name_s)
                    elif user_id:
                        user = (id_part, user_id)
            if bot and user:
                break
        if user is None and user_id:
            user = (user_id, user_id)
        return (bot, user)

    def _session_user_id(self) -> str:
        """从默认会话 umo 里取用户数字账号（排除 bot 账号）。"""
        return self._session_user_id_for(self.default_session_id, self.bot_sender_id)

    def _known_user_id(self) -> str:
        uid = self._session_user_id()
        if uid:
            return uid
        for key in self.user_display_names:
            value = str(key or "").strip()
            if value:
                return value
        return ""

    def _qq_identity_available(self) -> bool:
        if self.usage_mode == "local":
            return False
        try:
            for inst in self.context.platform_manager.get_insts():
                try:
                    meta = inst.meta()
                except Exception:
                    continue
                if str(getattr(meta, "name", "") or "").strip().casefold() == "aiocqhttp":
                    return True
        except Exception:
            pass
        return bool(self.bot_sender_id and self._known_user_id())

    def _identity_platform_for_session(
        self, session_id: str, platform: Any = None
    ) -> str:
        platform_name = self._session_platform(session_id, platform)
        if platform_name == LOCAL_PLATFORM and self._qq_identity_available():
            return "aiocqhttp"
        return platform_name

    def _normalize_identity_key(self, platform: Any, sender_id: Any) -> str | None:
        """归一成 "<平台>:<ID>"；平台缺省用 local，ID 为空才判为无效。"""
        platform_norm = self._normalize_platform_name(platform)
        if platform_norm not in SUPPORTED_PLATFORMS:
            return None
        sender_norm = str(sender_id or "").strip()
        if not sender_norm:
            return None
        return f"{platform_norm}:{sender_norm}"

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

    async def _run_reflection(
        self, session_id: str, persona_id: str | None = None
    ) -> dict[str, Any]:
        """按 LivingMemory 的规则判断是否该总结；需要时调度后台任务。"""
        persona = str(persona_id or "").strip() or self.default_persona_id
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
                persona,
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

    def _persist_migrated_bot_aliases(self, aliases: list[str]) -> None:
        """把旧的逗号串别名迁移成 list 并写回配置；失败不影响运行。"""
        if not aliases:
            return
        patch = {"bot_aliases_text": aliases}
        try:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver(patch)
                logger.info(
                    f"[LivingMemoryBridge] 已从旧文本迁移 {len(aliases)} 个别名到列表"
                )
                return
            async_saver = getattr(self.config, "save_config_async", None)
            if callable(async_saver):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.warning("[LivingMemoryBridge] 旧别名回写失败：没有运行中的事件循环")
                    return

                def _done(task: asyncio.Task) -> None:
                    try:
                        if task.result():
                            logger.info(
                                f"[LivingMemoryBridge] 已从旧文本迁移 {len(aliases)} 个别名到列表"
                            )
                        else:
                            logger.warning("[LivingMemoryBridge] 旧别名回写配置未提交")
                    except Exception as e:
                        logger.warning(f"[LivingMemoryBridge] 旧别名回写失败: {e}")

                task = loop.create_task(async_saver(patch))
                task.add_done_callback(_done)
                return
            logger.warning("[LivingMemoryBridge] 旧别名回写失败：配置对象没有保存接口")
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 旧别名回写失败: {e}")

    def _persist_migrated_user_map(self, entries: list[str]) -> None:
        """把旧文本迁移成 user_map list 并写回配置；只写一次，失败不影响运行。"""
        if not entries:
            return
        patch = {"user_map": entries}
        try:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver(patch)
                logger.info(
                    f"[LivingMemoryBridge] 已从旧映射迁移 {len(entries)} 条到「谁是谁」"
                )
                return
            async_saver = getattr(self.config, "save_config_async", None)
            if callable(async_saver):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.warning("[LivingMemoryBridge] 旧映射回写失败：没有运行中的事件循环")
                    return

                def _done(task: asyncio.Task) -> None:
                    try:
                        if task.result():
                            logger.info(
                                f"[LivingMemoryBridge] 已从旧映射迁移 {len(entries)} 条到「谁是谁」"
                            )
                        else:
                            logger.warning("[LivingMemoryBridge] 旧映射回写配置未提交")
                    except Exception as e:
                        logger.warning(f"[LivingMemoryBridge] 旧映射回写失败: {e}")

                task = loop.create_task(async_saver(patch))
                task.add_done_callback(_done)
                return
            logger.warning("[LivingMemoryBridge] 旧映射回写失败：配置对象没有保存接口")
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 旧映射回写失败: {e}")

    @staticmethod
    def _parse_user_map(raw: Any) -> dict[str, str]:
        """解析 user_map：list 每行「QQ号=名字,别名...」；兼容旧 dict 形态。"""
        out: dict[str, str] = {}

        def add(uid_value: Any, name_value: Any) -> None:
            uid = str(uid_value or "").strip()
            if not uid:
                return
            if not uid.isdigit() or len(uid) < 5:
                logger.warning(
                    f"[LivingMemoryBridge] user_map 条目不是有效 QQ 号，已忽略: {uid}"
                )
                return
            if isinstance(name_value, (list, tuple)):
                name_value = ",".join(str(x) for x in name_value)
            parts = [
                p.strip()
                for p in re.split(r"[,，/|、]", str(name_value or ""))
                if p.strip()
            ]
            if parts:
                out[uid] = "/".join(parts)

        if isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if not text or "=" not in text:
                    continue
                uid, name_text = text.split("=", 1)
                add(uid, name_text)
        elif isinstance(raw, dict):
            # 兼容上一版 dict 形态
            for uid, name_text in raw.items():
                add(uid, name_text)
        return out

    @staticmethod
    def _parse_alias_list(value: Any) -> list[str]:
        """解析桌宠别名：兼容 list 与旧的逗号串。"""
        if isinstance(value, str):
            parts = value.split(",")
        elif isinstance(value, (list, tuple)):
            parts = []
            for item in value:
                parts.extend(str(item or "").split(","))
        else:
            return []
        out: list[str] = []
        for part in parts:
            alias = part.strip()
            if alias and alias not in out:
                out.append(alias)
        return out

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
        self, session_id: str, persona_id: str, platform: Any = None
    ) -> list[dict[str, Any]]:
        """从 session 解析参与者，供建 person 节点与边。

        identity_key 统一成 platform:id（与上游反思同款写法）；platform 从 umo
        或消息字段解析，未知来源归入 local；bot 按会话取，用户取 session
        里的数字账号；昵称只进 display_name / aliases。
        """
        platform_name = self._session_platform(session_id, platform)
        if self.usage_mode == "local":
            return []
        identity_platform = self._identity_platform_for_session(session_id, platform_name)
        parts = [p for p in str(session_id or "").split(":") if p]
        bot_id, bot_name = self._bot_identity_for_session(session_id)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(sender_id: str, name: str, is_bot: bool) -> None:
            sender = str(sender_id or "").strip()
            identity_key = self._normalize_identity_key(identity_platform, sender)
            if not identity_key or identity_key in seen:
                return
            seen.add(identity_key)
            aliases = list(self.bot_aliases) if is_bot else [
                str(a).strip()
                for a in (self.user_alias_cfg.get(sender) or [])
                if str(a).strip()
            ]
            display_name = (name or "").strip() or sender
            out.append(
                {
                    "identity_key": identity_key,
                    "display_name": display_name,
                    "sender_id": sender,
                    "platform": identity_platform,
                    "aliases": [a for a in aliases if a and a != display_name],
                    "is_bot": is_bot,
                }
            )

        if platform_name == LOCAL_PLATFORM and identity_platform == "aiocqhttp":
            if bot_id:
                add(bot_id, self.user_display_names.get(bot_id, bot_name or bot_id), True)
            qq_user = self._known_user_id()
            if qq_user:
                add(qq_user, self.user_display_names.get(qq_user, qq_user), False)
            return out

        if bot_id:
            add(
                bot_id,
                self.user_display_names.get(bot_id, bot_name or bot_id),
                True,
            )
        for part in parts[1:]:
            if part.isdigit() and part != bot_id:
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
        platform: Any = None,
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
            normalized: list[dict[str, Any]] = []
            session_platform = self._identity_platform_for_session(session_id, platform)
            for item in participants:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("identity_key") or "").strip()
                platform_part, sep, id_part = key.partition(":")
                if not sep:
                    # 兼容只给 sender_id 的调用方
                    platform_part = session_platform
                    id_part = str(item.get("sender_id") or key).strip()
                normalized_key = self._normalize_identity_key(platform_part, id_part)
                if not normalized_key:
                    continue
                normalized.append(
                    {
                        **item,
                        "identity_key": normalized_key,
                        "platform": platform_part.casefold(),
                        "sender_id": id_part,
                    }
                )
            md["participant_identities"] = normalized

        md.setdefault("canonical_summary", content[:400])
        md.setdefault("key_facts", self._split_sentences(content) or [content[:400]])
        md.setdefault("topics", self._extract_topics(content))
        md.setdefault(
            "participant_identities",
            self._participants_from_session(session_id, persona_id, platform),
        )
        return md

    async def migrate_persona(
        self, old_persona: str, new_persona: str, dry_run: bool = True
    ) -> dict[str, Any]:
        """把旧人格标签迁移到新人格（documents / graph_entries / memory_atoms）。"""
        old = str(old_persona or "").strip()
        new = str(new_persona or "").strip()
        if not old or not new:
            return {"ok": False, "error": "需要提供旧人格和新人格"}
        if old == new:
            return {"ok": False, "error": "新旧人格相同"}
        engine = self._get_memory_engine()
        conn = getattr(engine, "db_connection", None) if engine else None
        if conn is None:
            return {"ok": False, "error": "记忆引擎数据库连接未就绪"}
        try:
            async def _count(sql: str) -> int:
                cursor = await conn.execute(sql, (old,))
                row = await cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0

            docs = await _count(
                "SELECT COUNT(*) FROM documents "
                "WHERE json_extract(metadata,'$.persona_id')=?"
            )
            entries = await _count(
                "SELECT COUNT(*) FROM graph_entries WHERE persona_id=?"
            )
            atoms = await _count(
                "SELECT COUNT(*) FROM memory_atoms WHERE persona_id=?"
            )
            result: dict[str, Any] = {
                "ok": True,
                "old": old,
                "new": new,
                "dry_run": dry_run,
                "documents": docs,
                "graph_entries": entries,
                "memory_atoms": atoms,
            }
            if dry_run or not (docs or entries or atoms):
                return result
            await conn.execute(
                "UPDATE documents SET metadata=json_set(metadata,'$.persona_id',?) "
                "WHERE json_extract(metadata,'$.persona_id')=?",
                (new, old),
            )
            await conn.execute(
                "UPDATE graph_entries SET persona_id=?, "
                "metadata=json_set(metadata,'$.persona_id',?) WHERE persona_id=?",
                (new, new, old),
            )
            await conn.execute(
                "UPDATE memory_atoms SET persona_id=? WHERE persona_id=?",
                (new, old),
            )
            await conn.commit()
            result["migrated"] = True
            logger.info(
                f"[LivingMemoryBridge] 人格迁移完成: {old} -> {new}, "
                f"documents={docs}, graph_entries={entries}, atoms={atoms}"
            )
            return result
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] 人格迁移失败: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}

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
                "plugin": "astrbot_plugin_coopanion_livingmemory",
                "version": VERSION,
                "livingmemory_connected": connected,
                "reflection_available": self._get_reflection() is not None,
                "auto_reflection": self.enable_auto_reflection,
                "usage_mode": self.usage_mode,
                "default_session_id": self.default_session_id,
                "default_persona_id": self.default_persona_id,
                "memory_share_mode": self.memory_share_mode,
                "pet_persona_id": self._effective_pet_persona(),
                "persona_filtering": self._persona_filtering_enabled(),
                "persona_filtering_warning": self._persona_filtering_warning,
                "mode_state": self._mode_state,
                "resolved_session_id": self.default_session_id,
                "resolved_persona_id": self._effective_persona(),
                "resolved_adapter_id": self._session_adapter_id(),
                "platform_type": self._platform_label(
                    self._session_platform(self.default_session_id)
                ),
                "persona_source": "auto" if self._persona_is_auto else "config",
                "identity_source": (
                    "auto"
                    if (self._persona_is_auto or not self._cfg_bot_qq)
                    else "config"
                ),
                "bot_identity": {
                    "id": self.bot_sender_id,
                    "name": self.bot_display_name,
                    "aliases": list(self.bot_aliases),
                },
                "user_identity": {
                    "id": self._session_user_id(),
                    "name": self.user_display_names.get(self._session_user_id(), ""),
                },
                "identity_warning": self._identity_warning,
                "persona_warning": self._persona_warning,
                "identity_key_format": "<平台>:<会话/用户ID>",
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
        persona_id = self._effective_persona(data.get("persona_id"))

        try:
            results = await self._search_with_read_scope(
                engine, query, limit, session_id, persona_id
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
        persona_id = self._effective_persona(data.get("persona_id"))
        platform = self._session_platform(session_id, data.get("platform"))
        if not self._is_supported_platform(platform):
            logger.warning(f"[LivingMemoryBridge] 来自 {platform} 的消息暂未支持，已跳过")
            return web.json_response(
                {"ok": False, "error": f"来自 {platform} 的消息暂未支持，已跳过"},
                status=400,
            )
        metadata = dict(data.get("metadata") or {})
        metadata.setdefault("platform", platform)
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
            platform=platform,
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
            persona_id = self._effective_persona(item.get("persona_id"))
            platform = self._session_platform(session_id, item.get("platform"))
            if not self._is_supported_platform(platform):
                errors.append({"index": idx, "error": f"来自 {platform} 的消息暂未支持，已跳过"})
                continue
            metadata = dict(item.get("metadata") or {})
            metadata.setdefault("platform", platform)
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
                platform=platform,
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
        session_id = str(data.get("session_id") or self.default_session_id).strip()
        persona_id = self._effective_persona(data.get("persona_id"))
        result = await self._run_reflection(
            self._chat_storage_session(session_id), persona_id
        )
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

        default_session_id = str(
            data.get("session_id")
            if "session_id" in data
            else self.default_session_id
        ).strip()
        default_platform = data.get("platform")
        default_persona_id = self._effective_persona(data.get("persona_id"))
        recorded = 0
        reflected: list[tuple[str, str]] = []
        skipped_identity = 0
        skipped_unsupported = 0

        for turn in turns:
            role = turn.get("role")
            content = str(turn.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue

            ts = turn.get("timestamp") or time.time()
            is_bot = role == "assistant"
            # 支持每个 turn 自带会话/人格/平台（群聊、多平台与多会话）
            session_id = str(
                turn.get("session_id")
                if "session_id" in turn
                else default_session_id
            ).strip()
            persona_id = (
                self._effective_persona(turn.get("persona_id"))
                if self.memory_share_mode == "pet_only"
                else str(turn.get("persona_id") or "").strip() or default_persona_id
            )
            storage_session_id = self._chat_storage_session(session_id)
            platform = self._session_platform(
                session_id, turn.get("platform") or default_platform
            )
            if not self._is_supported_platform(platform):
                skipped_unsupported += 1
                logger.warning(
                    f"[LivingMemoryBridge] 来自 {platform} 的消息暂未支持，已跳过"
                )
                continue
            identity_platform = self._identity_platform_for_session(session_id, platform)

            explicit_sender = ""
            identity_key = str(turn.get("identity_key") or "").strip()
            if identity_key:
                key_platform, sep, key_id = identity_key.partition(":")
                if sep and key_id.strip():
                    platform = self._normalize_platform_name(key_platform or platform)
                    explicit_sender = key_id.strip()
            if not explicit_sender:
                explicit_sender = str(
                    turn.get("sender_id") or turn.get("user_id") or ""
                ).strip()

            bot_id, bot_name = self._bot_identity_for_session(session_id)
            user_id = self._session_user_id_for(session_id, bot_id)
            if self.usage_mode == "local":
                bot_id = ""
                user_id = ""
            qq_user = self._known_user_id() if identity_platform == "aiocqhttp" else ""
            if platform == LOCAL_PLATFORM and identity_platform == "aiocqhttp":
                # 已接 QQ：桌宠本地通道也沿用 QQ 账号，不另造 local 假人
                if is_bot:
                    sender_id = bot_id
                    sender_name = bot_name or bot_id
                else:
                    sender_id = qq_user or explicit_sender
                    sender_name = (
                        str(turn.get("sender_name") or "").strip()
                        or self.user_display_names.get(sender_id, sender_id)
                    )
            else:
                if (
                    not is_bot
                    and not user_id
                    and platform == LOCAL_PLATFORM
                    and self.usage_mode != "local"
                ):
                    local_parts = [p for p in session_id.split(":") if p]
                    if len(local_parts) >= 2 and local_parts[-1] != "default":
                        user_id = local_parts[-1]
                if is_bot:
                    sender_id = explicit_sender or bot_id
                    sender_name = (
                        str(turn.get("sender_name") or "").strip()
                        or bot_name
                        or sender_id
                    )
                else:
                    sender_id = explicit_sender or user_id
                    sender_name = (
                        str(turn.get("sender_name") or "").strip()
                        or self.user_display_names.get(sender_id, sender_id)
                    )

            # 完全没接 QQ 时，本地通道才用固定本地身份兜底
            if not sender_id and platform == LOCAL_PLATFORM and session_id:
                sender_id = "assistant" if is_bot else "user"
                if not sender_name:
                    sender_name = self._fallback_sender_name(is_bot)

            if not sender_id:
                skipped_identity += 1
                logger.warning(
                    "[LivingMemoryBridge] 跳过一条无法归属的消息："
                    "没有平台、会话或发送者标识，无法建档"
                )
                continue

            normalized_key = self._normalize_identity_key(identity_platform, sender_id)
            try:
                await conv_mgr.add_message(
                    session_id=storage_session_id,
                    role=role,
                    content=content,
                    sender_id=sender_id,
                    sender_name=sender_name or sender_id,
                    platform=platform,
                    is_bot_message=is_bot,
                    metadata={
                        "source": "coopanion_desktop_pet",
                        "platform": platform,
                        "identity_platform": identity_platform,
                        "identity_key": normalized_key or "",
                    },
                    timestamp=ts,
                )
                recorded += 1
                key = (storage_session_id, persona_id)
                if key not in reflected:
                    reflected.append(key)
            except Exception as e:
                logger.warning(f"[LivingMemoryBridge] 记录对话单条失败: {e}")

        # 桌宠对话写入后按 LivingMemory 的规则判定是否蒸馏（达标才排后台总结任务）
        reflection: Any = None
        if self.enable_auto_reflection and recorded > 0:
            results: list[dict[str, Any]] = []
            for sid, pid in reflected:
                try:
                    results.append(await self._run_reflection(sid, pid))
                except Exception as e:
                    logger.warning(f"[LivingMemoryBridge] 自动蒸馏判定失败: {e}")
            if len(results) == 1:
                reflection = results[0]
            elif results:
                reflection = results

        payload: dict[str, Any] = {"ok": True, "recorded": recorded}
        if reflection is not None:
            payload["reflection"] = reflection
        if skipped_identity:
            payload["skipped_identity"] = skipped_identity
        if skipped_unsupported:
            payload["skipped_unsupported"] = skipped_unsupported
        return web.json_response(payload)

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
