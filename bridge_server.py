"""bridge_server.py - LivingMemory HTTP Bridge Server
提供本地 REST API 端点供 Coopanion 桌宠扩展进行高速直连交互
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from typing import Any

from aiohttp import web
from astrbot.api import logger
from astrbot.api.star import Context, StarTools

try:
    from .contract import (
        SCHEMA_VERSION,
        TOOLS_SCHEMA_VERSION,
        TOOLS_DEF,
        validate_tool_params,
    )
    from .upstream_adapter import UpstreamLivingMemoryAdapter
except ImportError:  # 兼容脚本/测试直接导入本模块
    from contract import (
        SCHEMA_VERSION,
        TOOLS_SCHEMA_VERSION,
        TOOLS_DEF,
        validate_tool_params,
    )
    from upstream_adapter import UpstreamLivingMemoryAdapter

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


VERSION = "0.2.2"

# ---- 多段检索（/api/memory/search 的可选 queries） ----
# 仅在调用方显式传 queries 时启用；不传时走的仍是原来的单段路径，行为一字不变。
_SEGMENT_SPLIT_RE = re.compile(r"[，。；！？,;!?\r\n]+")
_SEGMENT_MAX = 4
_SEGMENT_MIN_CHARS = 2
_SEGMENT_DEADLINE_SEC = 2.5
_SEGMENT_CACHE_TTL = 60.0
_RRF_K = 60
# 段权重：实测发现给首段更高权重会让「无锚点口语段」压过「关键信息段」，
# 把真正的命中（388）挤出 top3，所以这里取等权（RRF 原始定义即无权重融合）。
# 保留两个常量是为了以后按需调整，改这里即可。
_SEGMENT_WEIGHT_FIRST = 1.0
_SEGMENT_WEIGHT_REST = 1.0

# ---- 关键词子串路（路线 D）：只调上游公开方法 list_memories_page(keyword=...) ----
_KEYWORD_MAX = 6            # 单次最多提取几个关键词
_KEYWORD_BUDGET_SEC = 0.6   # 关键词路总预算，超时只用已返回部分
_KEYWORD_RRF_WEIGHT = 0.9   # 融合权重：略低于主路 1.0（子串精度高但覆盖窄）
_KEYWORD_CJK_MIN = 2        # 中文段最短 2 字
_KEYWORD_CJK_MAX = 6        # 中文段最长 6 字；更长的整段跳过（避免噪声）
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-/]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_NUM_ID_RE = re.compile(r"\d{4,}")
_KEYWORD_SEP_RE = re.compile(r"[-_./]+")


class LivingMemoryBridgeServer:
    # 自动识别可覆盖的"历史占位值"：这些值和空值一样视为尚未人工确认。
    _AUTO_SENTINELS = {
        "default_session_id": {"", "coopanion:default"},
        "default_persona_id": {"", "default"},
    }

    def __init__(self, context: Context, config: dict[str, Any]):
        self.context = context
        self.config = config
        self.upstream = UpstreamLivingMemoryAdapter(context)
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
        # 多段检索结果缓存：key -> (monotonic_ts, payload)
        self._segment_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # 关键词子串路开关（默认开；关掉后行为与加此路之前完全一致）
        self.keyword_boost_enabled = bool(config.get("recall_keyword_boost", True))
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

        # 单实例身份与生命周期状态机 (D-03, D-04, D-05)
        self.bridge_instance_id = (
            f"bridge_{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        )
        self.started_at = time.time()
        self.state = "starting"

        self._setup_routes()

    def _bridge_data_dir(self) -> str:
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_coopanion_livingmemory"))
            os.makedirs(data_dir, exist_ok=True)
            return data_dir
        except Exception:
            local_dir = os.path.join(os.path.dirname(__file__), "data")
            os.makedirs(local_dir, exist_ok=True)
            return local_dir

    def _lock_file_path(self) -> str:
        return os.path.join(self._bridge_data_dir(), "bridge-6198.lock")

    async def _probe_port(self) -> tuple[str, dict[str, Any] | None]:
        import aiohttp
        url = f"http://{self.host}:{self.port}/api/health"
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=1.2)) as resp:
                    if resp.status in (200, 401):
                        data = {}
                        try:
                            data = await resp.json()
                        except Exception:
                            pass
                        schema = data.get("schema_version")
                        if schema == SCHEMA_VERSION or data.get("plugin") == "astrbot_plugin_coopanion_livingmemory":
                            return "reuse", data
                        return "conflict", data
                    return "conflict", None
        except Exception:
            return "none", None

    def _acquire_lock(self) -> bool:
        lock_path = self._lock_file_path()
        lock_data = {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "bridge_instance_id": self.bridge_instance_id,
            "schema_version": SCHEMA_VERSION,
            "plugin_version": VERSION,
            "host": self.host,
            "port": self.port,
        }
        try:
            if os.path.exists(lock_path):
                try:
                    with open(lock_path, "r", encoding="utf-8") as f:
                        old_info = json.load(f)
                    old_pid = int(old_info.get("pid", 0))
                    if old_pid and old_pid != os.getpid():
                        try:
                            import psutil
                            if not psutil.pid_exists(old_pid):
                                logger.info(f"[LivingMemoryBridge] 清理失效进程锁 (stale pid={old_pid})")
                        except Exception:
                            pass
                except Exception:
                    pass
            with open(lock_path, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 写入锁文件失败: {e}")
            return False

    def _release_lock(self) -> None:
        lock_path = self._lock_file_path()
        try:
            if os.path.exists(lock_path):
                with open(lock_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                if info.get("bridge_instance_id") == self.bridge_instance_id:
                    os.remove(lock_path)
                    logger.debug("[LivingMemoryBridge] 进程锁已释放")
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 释放锁文件失败: {e}")

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
        self.app.router.add_get("/api/tools/manifest", self.handle_manifest)
        self.app.router.add_post("/api/memory/search", self.handle_search)
        self.app.router.add_post("/api/memory/add", self.handle_add)
        self.app.router.add_post("/api/memory/memorize", self.handle_memorize)
        self.app.router.add_post("/api/memory/recall", self.handle_recall)
        self.app.router.add_post("/api/memory/evidence", self.handle_evidence)
        self.app.router.add_post("/api/memory/forget", self.handle_forget)
        self.app.router.add_post("/api/memory/batch_add", self.handle_batch_add)
        self.app.router.add_post("/api/memory/chat", self.handle_chat)
        self.app.router.add_post("/api/memory/reflect", self.handle_reflect)

    # ------------------------------------------------------------------
    # 自动检测：让用户尽量不用填配置
    #
    # 会话：LivingMemory 的统计里已经按 session 列了记忆条数，取最多的那个。
    # 身份：上游会话历史里已有双方的账号与昵称（is_bot_message 区分）。
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
        raw = self.upstream.config_get(
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
        """每个会话的消息条数（由上游适配层读取）。"""
        return self.upstream.conversation_counts()

    def _session_personas(self) -> dict[str, str]:
        """每个会话在记忆里出现最多的人格标签（由上游适配层读取）。"""
        return self.upstream.session_personas()

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
            engine = self.upstream.engine()
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
        fallback = (self.bot_sender_id, self.bot_display_name)
        result = self.upstream.bot_identity(session_id, fallback)
        if result != fallback:
            self._bot_session_cache[session_id] = result
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
        engine = self.upstream.engine()
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
        persona = self.upstream.dominant_persona(session_id)
        if persona:
            return persona
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
        """从会话历史与现有记忆里读出 (bot_id, bot_name, user_id, user_name)。只读。"""
        return self.upstream.detect_identities(self._session_user_id())

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
    # 插件升级若改名，适配层的反思组件发现会失败并在回执里指出缺哪个符号。
    # ------------------------------------------------------------------

    async def _run_reflection(
        self, session_id: str, persona_id: str | None = None
    ) -> dict[str, Any]:
        """按 LivingMemory 的规则判断是否该总结；需要时调度后台任务。"""
        persona = str(persona_id or "").strip() or self.default_persona_id
        mr = self.upstream.reflection_component()
        cm = self.upstream.conversation_api()
        if mr is None:
            return {
                "ok": False,
                "error": "拿不到 LivingMemory 的记忆反思模块（插件可能升级改名）",
                "hint": "检查 astrbot_plugin_livingmemory 版本；桥的上游适配层需适配新的属性名",
            }
        if cm is None:
            return {"ok": False, "error": "ConversationManager 尚未就绪"}

        scope_mode = str(
            self.upstream.config_get("filtering_settings.memory_scope_mode", "legacy")
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
                self.upstream.config_get("reflection_engine.summary_trigger_rounds", 10)
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
        return await self.upstream.migrate_persona(
            self.upstream.engine(), old_persona, new_persona, dry_run
        )

    def _capability_block_response(
        self, kind: str, action: str
    ) -> web.Response | None:
        reason = self.upstream.write_block_reason(kind)
        if not reason:
            return None
        return web.json_response(
            {
                "ok": False,
                "error": f"{action}已拒绝：{reason}",
                "upstream": self.upstream.capability_summary(),
            },
            status=503,
        )

    def _write_block_response(self, kind: str) -> web.Response | None:
        return self._capability_block_response(kind, "写入")

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /api/health - 探针与状态"""
        # 健康检查主动失效能力缓存，避免启动期把“尚未初始化”永久当成“不兼容”。
        self.upstream.refresh()
        await self._ensure_auto()
        engine = self.upstream.engine()
        connected = engine is not None

        stats_data = {}
        if connected:
            try:
                stats = await engine.get_statistics()
                stats_data = stats if isinstance(stats, dict) else {}
            except Exception as e:
                stats_data = {"error": str(e)}

        capability = self.upstream.capability_summary()
        # 动态判定当前实例状态 (D-04)
        if self.state not in ("port_conflict", "failed", "external_owner"):
            if connected:
                self.state = "ready"
            else:
                self.state = "upstream_pending"

        return web.json_response(
            {
                "ok": True,
                "state": self.state,
                "bridge_instance_id": self.bridge_instance_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "schema_version": SCHEMA_VERSION,
                "tools_schema_version": TOOLS_SCHEMA_VERSION,
                "plugin_version": VERSION,
                "upstream": {
                    "ready": connected,
                    "version": capability["upstream_version"],
                    "capabilities": capability["capabilities"],
                },
                "plugin": "astrbot_plugin_coopanion_livingmemory",
                "version": VERSION,
                "upstream_version": capability["upstream_version"],
                "upstream_capabilities": capability["capabilities"],
                "upstream_capability_warning": capability["warning"],
                "livingmemory_connected": connected,
                "reflection_available": self.upstream.reflection_component() is not None,
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

    async def handle_manifest(self, request: web.Request) -> web.Response:
        """GET /api/tools/manifest - 动态暴露工具面定义与可用性目录 (A-02)"""
        self.upstream.refresh()
        tools_list = []
        catalog_list = []

        for t_name, t_def in TOOLS_DEF.items():
            avail, reason = self.upstream.get_tool_availability(t_name)
            tool_entry = {
                "name": t_def["name"],
                "endpoint": t_def["endpoint"],
                "http_method": t_def["http_method"],
                "capability": t_def["capability"],
                "mode": t_def["mode"],
                "permission": t_def["permission"],
                "tags": t_def["tags"],
                "description": t_def["description"],
                "parameters": t_def["parameters"],
            }
            catalog_entry = dict(tool_entry)
            catalog_entry["available"] = avail
            catalog_entry["reason"] = reason
            catalog_list.append(catalog_entry)

            if avail:
                tools_list.append(tool_entry)

        # 签名与 revision (LM-E2E-06 热更新检测)
        raw_rev = f"{SCHEMA_VERSION}_{VERSION}_{self.upstream.upstream_version()}_" + "_".join(t["name"] for t in tools_list)
        rev = hashlib.sha256(raw_rev.encode()).hexdigest()[:12]

        return web.json_response(
            {
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "tools_schema_version": TOOLS_SCHEMA_VERSION,
                "revision": rev,
                "bridge_version": VERSION,
                "upstream_version": self.upstream.upstream_version(),
                "tools": tools_list,
                "catalog": catalog_list,
            }
        )

    async def handle_search(self, request: web.Request) -> web.Response:
        """POST /api/memory/search - 多路混合语义与图谱检索 (A-01, mem_search)"""
        await self._ensure_auto()
        blocked = self._capability_block_response("search", "检索")
        if blocked is not None:
            return blocked
        engine = self.upstream.engine()
        if not engine:
            return web.json_response(
                {"ok": False, "code": "tool_not_ready", "error": "LivingMemory 引擎尚未就绪或不可用"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "code": "invalid_json", "error": "无效的 JSON 请求体"}, status=400
            )

        # 严格契约参数校验 (D-01, LM-E2E-18, LM-E2E-19)
        valid, err = validate_tool_params("mem_search", data)
        if not valid and err:
            return web.json_response(err, status=422)

        query = str(data.get("query", "")).strip()
        limit = max(1, min(20, int(data.get("limit", 5))))
        session_id = data.get("session_id") or self.default_session_id
        persona_id = self._effective_persona(data.get("persona_id"))

        # 可选多段检索：只有调用方显式传 queries 且超过 1 段时才走新路径，
        # 其余情况（含不传 queries）保持原来的单段实现，行为与改前完全一致。
        segments = self._normalize_queries(data.get("queries"))
        if not query and not segments:
            return web.json_response(
                {
                    "ok": False,
                    "code": "missing_field",
                    "error": "query 与 queries 至少提供一个",
                },
                status=422,
            )

        if len(segments) > 1:
            try:
                payload = await self._search_multi_segment(
                    engine, segments, limit, session_id, persona_id
                )
                return web.json_response(payload)
            except Exception as e:
                logger.error(f"[LivingMemoryBridge] 多段检索失败: {e}", exc_info=True)
                return web.json_response(
                    {"ok": False, "code": "internal_error", "error": str(e)}, status=500
                )

        attempt_query = query or (segments[0] if segments else "")
        try:
            results = await self._search_with_read_scope(
                engine, attempt_query, limit, session_id, persona_id
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

            # 关键词子串路：只补「向量/BM25 候选里根本没有」的精确命中
            if self.keyword_boost_enabled:
                keywords = self._extract_keywords(attempt_query)
                if keywords:
                    keyword_hits = await self._search_by_keywords(
                        engine, keywords, limit, session_id, persona_id
                    )
                    if keyword_hits:
                        memories = self._fuse_memories(memories, keyword_hits, limit)

            return web.json_response(
                {
                    "ok": True,
                    "query": attempt_query,
                    "count": len(memories),
                    "memories": memories,
                }
            )
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] 检索失败: {e}", exc_info=True)
            return web.json_response({"ok": False, "code": "internal_error", "error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # 多段检索（可选能力，仅在 queries 显式传入且 >=2 段时启用）
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_queries(raw: Any) -> list[str]:
        """把 queries 规整成去重、非空、长度达标的段列表（最多 4 段）。

        只接受字符串数组；任何非法元素直接忽略。返回空列表表示未提供多段。
        """
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if len(text) < _SEGMENT_MIN_CHARS:
                continue
            if text in out:
                continue
            out.append(text)
            if len(out) >= _SEGMENT_MAX:
                break
        return out

    @staticmethod
    def _split_query_segments(query: str) -> list[str]:
        """按中英文标点与换行切分长 query；供调用方未传 queries 时的自切使用。"""
        parts = [p.strip() for p in _SEGMENT_SPLIT_RE.split(str(query or ""))]
        out: list[str] = []
        for part in parts:
            if len(part) < _SEGMENT_MIN_CHARS:
                continue
            if part in out:
                continue
            out.append(part)
            if len(out) >= _SEGMENT_MAX:
                break
        return out

    # ------------------------------------------------------------------
    # 关键词子串路（路线 D）：从 query 提关键词 → 上游公开方法 list_memories_page
    # ------------------------------------------------------------------

    @staticmethod
    def _keyword_variants(token: str) -> list[str]:
        """D+ 第 1 项：复合标识给双形态 + 小写变体。

        - 原串（最高优先）
        - 按 `- _ . /` 拆出的子 token（取前两个，长度 ≥2）
        - 小写变体
        纯字符串处理，不引入任何新依赖。
        """
        out: list[str] = []

        def push(value: str) -> None:
            value = value.strip()
            if len(value) >= 2 and value not in out:
                out.append(value)

        push(token)
        parts = [p for p in _KEYWORD_SEP_RE.split(token) if len(p) >= 2]
        if len(parts) >= 2:
            for part in parts[:2]:
                push(part)
        push(token.lower())
        return out

    def _extract_keywords(self, query: str) -> list[str]:
        """从 query 抽取适合做子串匹配的片段。

        规则（与调研文档 §四 一致）：
        1. 英文/数字复合标识（≥3 字符，允许 `- _ . /`）；
        2. 中文连续段，长度 2~6 直接取；更长的跳过（整句 LIKE 命中率低且噪声大）；
        3. 4 位以上纯数字（记忆 ID / 账号）。
        """
        text = str(query or "")
        out: list[str] = []

        def push(value: str) -> None:
            value = value.strip()
            if len(value) >= 2 and value not in out:
                out.append(value)

        for match in _ASCII_TOKEN_RE.finditer(text):
            for variant in self._keyword_variants(match.group(0)):
                push(variant)
                if len(out) >= _KEYWORD_MAX:
                    return out

        for match in _CJK_RUN_RE.finditer(text):
            run = match.group(0)
            if _KEYWORD_CJK_MIN <= len(run) <= _KEYWORD_CJK_MAX:
                push(run)
            if len(out) >= _KEYWORD_MAX:
                return out

        for match in _NUM_ID_RE.finditer(text):
            push(match.group(0))
            if len(out) >= _KEYWORD_MAX:
                return out

        return out[:_KEYWORD_MAX]

    async def _search_by_keywords(
        self,
        engine: Any,
        keywords: list[str],
        limit: int,
        session_id: str,
        persona_id: str,
    ) -> dict[int, dict[str, Any]]:
        """并行调用上游 list_memories_page(keyword=...)，返回 {documents.id: 命中信息}。

        只使用上游公开方法；任何单关键词失败都只跳过它。
        """
        if not keywords or not hasattr(engine, "list_memories_page"):
            return {}

        tasks: dict[asyncio.Task, str] = {}
        for keyword in keywords:
            try:
                coro = engine.list_memories_page(
                    session_id=session_id or None,
                    keyword=keyword,
                    status="active",
                    page=1,
                    page_size=max(limit * 2, 10),
                )
            except TypeError:
                # 老版本签名不接受某些参数：退化为最少参数调用
                coro = engine.list_memories_page(keyword=keyword, page=1, page_size=max(limit * 2, 10))
            tasks[asyncio.create_task(coro)] = keyword

        done, pending = await asyncio.wait(tasks.keys(), timeout=_KEYWORD_BUDGET_SEC)
        for task in pending:
            task.cancel()

        hits: dict[int, dict[str, Any]] = {}
        for task in done:
            keyword = tasks[task]
            try:
                page_payload = task.result() or {}
            except Exception as exc:  # 单个关键词失败不影响其它
                logger.debug("[LivingMemoryBridge] 关键词 %r 检索失败: %s", keyword, exc)
                continue
            items = page_payload.get("items") or []
            for position, item in enumerate(items):
                doc_id = item.get("id")
                if doc_id is None:
                    continue
                entry = hits.setdefault(
                    doc_id, {"item": item, "keywords": [], "score": 0.0}
                )
                if keyword not in entry["keywords"]:
                    entry["keywords"].append(keyword)
                    entry["score"] += _KEYWORD_RRF_WEIGHT / (_RRF_K + position + 1)
        return hits

    @staticmethod
    def _fuse_memories(
        main_memories: list[dict[str, Any]],
        keyword_hits: dict[int, dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        """主路与关键词路 RRF 融合。

        主路按名次折算 RRF 分（与关键词路同量级），关键词路每个命中词累加；
        同时被两路命中的文档自然分更高。结果标 `from_keywords`。
        """
        fused: dict[int, dict[str, Any]] = {}

        for rank, memory in enumerate(main_memories):
            doc_id = memory.get("id")
            if doc_id is None:
                continue
            fused[doc_id] = {
                "id": doc_id,
                "content": memory.get("content") or "",
                "score": 1.0 / (_RRF_K + rank + 1),
                "metadata": dict(memory.get("metadata") or {}),
            }

        for doc_id, hit in keyword_hits.items():
            item = hit.get("item") or {}
            raw_metadata = item.get("metadata")
            if isinstance(raw_metadata, str):
                try:
                    raw_metadata = json.loads(raw_metadata)
                except Exception:
                    raw_metadata = {}
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            metadata["from_keywords"] = list(hit.get("keywords") or [])

            existing = fused.get(doc_id)
            if existing is not None:
                existing["score"] += float(hit.get("score") or 0.0)
                existing["metadata"]["from_keywords"] = metadata["from_keywords"]
            else:
                fused[doc_id] = {
                    "id": doc_id,
                    "content": item.get("text") or "",
                    "score": float(hit.get("score") or 0.0),
                    "metadata": metadata,
                }

        ordered = sorted(fused.values(), key=lambda entry: entry["score"], reverse=True)
        return [
            {
                "id": entry["id"],
                "content": entry["content"],
                "score": round(float(entry["score"]), 6),
                "metadata": entry["metadata"],
            }
            for entry in ordered[:limit]
        ]

    async def _search_multi_segment(
        self,
        engine: Any,
        segments: list[str],
        limit: int,
        session_id: str,
        persona_id: str,
    ) -> dict[str, Any]:
        """逐段并行检索，min-max 归一分后按 RRF 融合；整批硬超时 2.5s。

        超时后已到达的段照常参与融合并标 `partial: true`；完整结果缓存 60s。
        """
        cache_key = f"{'|'.join(segments)}|{persona_id}|{limit}"
        now = time.monotonic()
        cached = self._segment_cache.get(cache_key)
        if cached is not None and now - cached[0] < _SEGMENT_CACHE_TTL:
            return dict(cached[1])

        tasks: dict[asyncio.Task, int] = {}
        for index, segment in enumerate(segments):
            task = asyncio.create_task(
                self._search_with_read_scope(
                    engine, segment, limit, session_id, persona_id
                )
            )
            tasks[task] = index

        done, pending = await asyncio.wait(
            tasks.keys(), timeout=_SEGMENT_DEADLINE_SEC
        )
        partial = bool(pending)
        for task in pending:
            task.cancel()

        by_segment: dict[int, list[Any]] = {}
        for task in done:
            index = tasks[task]
            try:
                by_segment[index] = task.result() or []
            except Exception as exc:  # 单段失败不影响其它段
                logger.warning(f"[LivingMemoryBridge] 第 {index + 1} 段检索失败: {exc}")
                by_segment[index] = []

        merged: dict[Any, dict[str, Any]] = {}
        for index in sorted(by_segment):
            rows = by_segment[index]
            if not rows:
                continue
            weight = _SEGMENT_WEIGHT_FIRST if index == 0 else _SEGMENT_WEIGHT_REST  # 当前等权
            scores = [float(getattr(r, "final_score", 0.0) or 0.0) for r in rows]
            low, high = min(scores), max(scores)
            span = (high - low) or 1.0
            normalized = [(s - low) / span for s in scores]
            ranked = sorted(
                range(len(rows)), key=lambda i: normalized[i], reverse=True
            )
            for rank, row_index in enumerate(ranked):
                row = rows[row_index]
                key = getattr(row, "doc_id", None)
                if key is None:
                    key = id(row)
                entry = merged.setdefault(
                    key, {"row": row, "score": 0.0, "segments": []}
                )
                entry["score"] += weight / (_RRF_K + rank + 1)
                entry["segments"].append(index + 1)

        ordered = sorted(
            merged.values(), key=lambda e: e["score"], reverse=True
        )[:limit]

        memories: list[dict[str, Any]] = []
        for entry in ordered:
            row = entry["row"]
            metadata = dict(getattr(row, "metadata", {}) or {})
            metadata["from_segments"] = entry["segments"]
            memories.append(
                {
                    "id": getattr(row, "doc_id", None),
                    "content": getattr(row, "content", ""),
                    "score": round(entry["score"], 6),
                    "metadata": metadata,
                }
            )

        # 关键词子串路：与多段结果再融合一次（扩展端实际走的就是这条路径）
        if self.keyword_boost_enabled:
            keywords: list[str] = []
            for segment in segments:
                for keyword in self._extract_keywords(segment):
                    if keyword not in keywords:
                        keywords.append(keyword)
                if len(keywords) >= _KEYWORD_MAX:
                    break
            keywords = keywords[:_KEYWORD_MAX]
            if keywords:
                keyword_hits = await self._search_by_keywords(
                    engine, keywords, limit, session_id, persona_id
                )
                if keyword_hits:
                    memories = self._fuse_memories(memories, keyword_hits, limit)

        payload: dict[str, Any] = {
            "ok": True,
            "query": " | ".join(segments),
            "count": len(memories),
            "partial": partial,
            "memories": memories,
        }
        if not partial:
            self._segment_cache[cache_key] = (now, dict(payload))
        return payload

    async def handle_add(self, request: web.Request) -> web.Response:
        """POST /api/memory/add - 添加记忆记录 (轻量直写，mem_record)"""
        await self._ensure_auto()
        blocked = self._write_block_response("memory")
        if blocked is not None:
            return blocked
        engine = self.upstream.engine()
        if not engine:
            return web.json_response(
                {"ok": False, "code": "tool_not_ready", "error": "LivingMemory 引擎尚未就绪或不可用"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "code": "invalid_json", "error": "无效的 JSON 请求体"}, status=400
            )

        # 严格契约校验 (D-01, LM-E2E-20)
        valid, err = validate_tool_params("mem_record", data)
        if not valid and err:
            return web.json_response(err, status=422)

        content = str(data.get("content", "")).strip()
        importance = float(data.get("importance", 0.6))
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
        # 区分两条写入通道 (2.2 & LM-E2E-08)
        metadata["memory_origin"] = "pet_mem_record"
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
            return web.json_response({"ok": False, "code": "internal_error", "error": str(e)}, status=500)

    async def handle_memorize(self, request: web.Request) -> web.Response:
        """POST /api/memory/memorize - 深度结构化记忆沉淀 (A-03, mem_memorize)"""
        await self._ensure_auto()
        avail, reason = self.upstream.is_memorize_enabled()
        if not avail:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_disabled",
                    "tool": "mem_memorize",
                    "message": reason,
                    "reason": reason,
                },
                status=403,
            )
        engine = self.upstream.engine()
        processor = self.upstream.memory_processor()
        if not engine or not processor:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_not_ready",
                    "tool": "mem_memorize",
                    "message": "记忆引擎或处理器未就绪",
                },
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "code": "invalid_json", "message": "无效的 JSON 请求体"},
                status=400,
            )

        valid, err = validate_tool_params("mem_memorize", data)
        if not valid and err:
            return web.json_response(err, status=422)

        content = str(data.get("content", "")).strip()
        topics = data.get("topics") or []
        key_facts = data.get("key_facts") or []
        sentiment = str(data.get("sentiment") or "neutral").strip().lower()
        if sentiment not in {"positive", "neutral", "negative"}:
            sentiment = "neutral"
        importance = float(data.get("importance", 0.7))
        reason_text = str(data.get("reason", "")).strip()
        session_id = str(data.get("session_id") or self.default_session_id)
        persona_id = self._effective_persona(data.get("persona_id"))

        is_group_chat = "GroupMessage" in session_id
        structured_data = {
            "summary": content,
            "topics": [str(t).strip() for t in topics if str(t).strip()][:5],
            "key_facts": [str(f).strip() for f in key_facts if str(f).strip()][:5],
            "sentiment": sentiment,
            "importance": importance,
        }

        try:
            processed_content, metadata, normalized_importance = (
                processor.build_memory_from_structured_data(
                    structured_data=structured_data,
                    is_group_chat=is_group_chat,
                    fallback_excerpt=content,
                )
            )
            metadata["source_window"] = {
                "session_id": session_id,
                "triggered_by": "bridge_tool",
                "tool_name": "mem_memorize",
            }
            metadata["memory_origin"] = "bridge_memorize"
            metadata["source_session_id"] = session_id
            if reason_text:
                metadata["memorize_reason"] = reason_text

            memory_id = await engine.add_memory(
                content=processed_content,
                session_id=session_id,
                persona_id=persona_id,
                importance=normalized_importance,
                metadata=metadata,
            )
            return web.json_response(
                {
                    "ok": True,
                    "memory_id": memory_id,
                    "content": processed_content,
                    "importance": normalized_importance,
                    "session_id": session_id,
                    "persona_id": persona_id,
                    "topics": structured_data["topics"],
                    "status": "memorized",
                }
            )
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] memorize 写入失败: {e}", exc_info=True)
            return web.json_response(
                {"ok": False, "code": "internal_error", "message": str(e)},
                status=500,
            )

    async def handle_recall(self, request: web.Request) -> web.Response:
        """POST /api/memory/recall - 专注精准召回 (A-04, mem_recall)"""
        await self._ensure_auto()
        avail, reason = self.upstream.is_recall_enabled()
        if not avail:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_disabled",
                    "tool": "mem_recall",
                    "message": reason,
                    "reason": reason,
                },
                status=403,
            )
        engine = self.upstream.engine()
        if not engine:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_not_ready",
                    "tool": "mem_recall",
                    "message": "记忆引擎尚未就绪",
                },
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "code": "invalid_json", "message": "无效的 JSON 请求体"},
                status=400,
            )

        valid, err = validate_tool_params("mem_recall", data)
        if not valid and err:
            return web.json_response(err, status=422)

        query = str(data.get("query", "")).strip()
        limit = max(1, min(20, int(data.get("limit", 5))))
        include_source = bool(data.get("include_source", False))
        target = str(data.get("target") or "conversation").strip()
        session_id = str(data.get("session_id") or self.default_session_id)
        persona_id = self._effective_persona(data.get("persona_id"))

        if target == "current_speaker":
            beta_ok, beta_reason = self.upstream.is_agentic_recall_enabled()
            if not beta_ok:
                return web.json_response(
                    {
                        "ok": False,
                        "code": "tool_disabled",
                        "tool": "mem_recall",
                        "message": "target='current_speaker' 模式需要开启 agentic recall beta",
                        "reason": beta_reason,
                    },
                    status=403,
                )

        try:
            memories = await engine.search_memories(
                query=query,
                k=limit,
                session_id=session_id,
                persona_id=persona_id,
            )
            serialized = []
            for m in memories:
                meta = m.metadata if isinstance(m.metadata, dict) else {}
                item = {
                    "id": getattr(m, "doc_id", None),
                    "content": getattr(m, "content", ""),
                    "score": getattr(m, "final_score", 0.0),
                    "importance": meta.get("importance"),
                    "session_id": meta.get("session_id"),
                    "persona_id": meta.get("persona_id"),
                    "create_time": meta.get("create_time"),
                }
                if include_source and meta.get("has_source"):
                    get_source = getattr(engine, "get_memory_source", None)
                    if callable(get_source):
                        item["source_messages"] = await get_source(m.doc_id)
                serialized.append(item)

            return web.json_response(
                {
                    "ok": True,
                    "query": query,
                    "count": len(serialized),
                    "results": serialized,
                }
            )
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] recall 失败: {e}", exc_info=True)
            return web.json_response(
                {"ok": False, "code": "internal_error", "message": str(e)},
                status=500,
            )

    async def handle_evidence(self, request: web.Request) -> web.Response:
        """POST /api/memory/evidence - 按 memory_id 深读记忆事实与原文 (A-05, mem_evidence)"""
        await self._ensure_auto()
        avail, reason = self.upstream.is_evidence_enabled()
        if not avail:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_disabled",
                    "tool": "mem_evidence",
                    "message": reason,
                    "reason": reason,
                },
                status=403,
            )
        engine = self.upstream.engine()
        if not engine:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_not_ready",
                    "tool": "mem_evidence",
                    "message": "记忆引擎尚未就绪",
                },
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "code": "invalid_json", "message": "无效的 JSON 请求体"},
                status=400,
            )

        valid, err = validate_tool_params("mem_evidence", data)
        if not valid and err:
            return web.json_response(err, status=422)

        memory_id = int(data.get("memory_id"))
        include_source = bool(data.get("include_source", False))
        cursor = str(data.get("cursor", "")).strip()

        try:
            import sys
            from pathlib import Path
            try:
                from astrbot_plugin_livingmemory.core.tools.memory_evidence_reader import read_memory_evidence_core
                from astrbot_plugin_livingmemory.core.tools.memory_evidence_output import paginate_evidence
            except ImportError:
                up_dir = str(Path(self.upstream._data_dir()).parent.parent / "plugins" / "astrbot_plugin_livingmemory")
                if up_dir not in sys.path:
                    sys.path.insert(0, up_dir)
                from core.tools.memory_evidence_reader import read_memory_evidence_core
                from core.tools.memory_evidence_output import paginate_evidence

            doc = await read_memory_evidence_core(
                memory_engine=engine,
                recall_session_id=self.default_session_id,
                recall_persona_id=self.default_persona_id,
                memory_id=memory_id,
                include_source=include_source,
            )
            if doc.get("status") == "not_found":
                return web.json_response(
                    {"ok": False, "code": "not_found", "message": f"记忆 ID {memory_id} 未找到或不可见"},
                    status=404,
                )
            if cursor:
                doc = paginate_evidence(doc, cursor=cursor)
            return web.json_response({"ok": True, **doc})
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] evidence 读取失败: {e}", exc_info=True)
            return web.json_response(
                {"ok": False, "code": "internal_error", "message": str(e)},
                status=500,
            )

    async def handle_forget(self, request: web.Request) -> web.Response:
        """POST /api/memory/forget - 记忆软删除/归档 (A-06, mem_forget)"""
        await self._ensure_auto()
        avail, reason = self.upstream.is_forget_enabled()
        if not avail:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_disabled",
                    "tool": "mem_forget",
                    "message": reason,
                    "reason": reason,
                },
                status=403,
            )
        engine = self.upstream.engine()
        if not engine:
            return web.json_response(
                {
                    "ok": False,
                    "code": "tool_not_ready",
                    "tool": "mem_forget",
                    "message": "记忆引擎尚未就绪",
                },
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "code": "invalid_json", "message": "无效的 JSON 请求体"},
                status=400,
            )

        valid, err = validate_tool_params("mem_forget", data)
        if not valid and err:
            return web.json_response(err, status=422)

        memory_ids = data.get("memory_ids") or []
        if not memory_ids:
            return web.json_response(
                {
                    "ok": False,
                    "code": "missing_field",
                    "field": "memory_ids",
                    "message": "memory_ids 不能为空",
                },
                status=422,
            )

        max_memories = int(self.upstream.config_get("agent_tools.forget_tool_max_memories", 3))
        if len(memory_ids) > max_memories:
            return web.json_response(
                {
                    "ok": False,
                    "code": "limit_exceeded",
                    "message": f"单次最多只能归档 {max_memories} 条记忆，实际传入 {len(memory_ids)} 条",
                    "max_memories": max_memories,
                },
                status=400,
            )

        # 权限校验：仅管理员可调用 (LM-E2E-11)
        # 配置缺失时不放行，避免未设置 admins_id 时任何人都能归档记忆。
        requester_id = str(data.get("user_id") or self._session_user_id() or "")
        admins: list[str] = []
        if self.context and hasattr(self.context, "astrbot_config"):
            raw_admins = self.context.astrbot_config.get("admins_id") or []
            admins = [str(a).strip() for a in raw_admins if str(a).strip()]
        if not admins or (requester_id and requester_id not in admins):
            return web.json_response(
                {
                    "ok": False,
                    "code": "permission_denied",
                    "message": "未配置管理员或当前用户无权限执行遗忘操作",
                },
                status=403,
            )

        reason_text = str(data.get("reason", "")).strip()
        try:
            archived_count = await engine.archive_memories(
                memory_ids,
                audit={
                    "archived_by": "bridge_tool",
                    "archive_reason": reason_text,
                    "session_id": self.default_session_id,
                },
            )
            return web.json_response(
                {
                    "ok": True,
                    "action": "archived",
                    "archived_count": archived_count,
                    "memory_ids": memory_ids,
                    "audit": {
                        "reason": reason_text,
                        "archived_by": "bridge_tool",
                    },
                }
            )
        except Exception as e:
            logger.error(f"[LivingMemoryBridge] 归档记忆失败: {e}", exc_info=True)
            return web.json_response(
                {"ok": False, "code": "internal_error", "message": str(e)},
                status=500,
            )

    async def handle_batch_add(self, request: web.Request) -> web.Response:
        """POST /api/memory/batch_add - 批量添加记忆（用于初始化迁移与批量同步）"""
        await self._ensure_auto()
        blocked = self._write_block_response("memory")
        if blocked is not None:
            return blocked
        engine = self.upstream.engine()
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
        blocked = self._write_block_response("reflect")
        if blocked is not None:
            return blocked
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
        blocked = self._write_block_response("chat")
        if blocked is not None:
            return blocked
        conv_mgr = self.upstream.conversation_api()
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
        """启动 HTTP 桥服务，实现单实例锁与状态机 (D-04, D-05)"""
        self.state = "starting"
        # 1. 探测端口是否已被活实例占用
        status, data = await self._probe_port()
        if status == "reuse":
            self.state = "external_owner"
            ext_inst = data.get("bridge_instance_id") if data else "unknown"
            logger.info(
                f"[LivingMemoryBridge] 检测到同协议活实例已在运行 (instance={ext_inst})，复用现有服务 (external_owner)"
            )
            return
        elif status == "conflict":
            self.state = "port_conflict"
            logger.warning(
                f"[LivingMemoryBridge] 端口 {self.port} 被外部未知服务占用，状态置为 port_conflict，禁止强杀外部进程"
            )
            return

        # 2. 抢锁
        self._acquire_lock()

        # 3. 启动 aiohttp 服务
        try:
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
            if self.upstream.capability("engine").enabled:
                self.state = "ready"
            else:
                self.state = "upstream_pending"
            logger.info(
                f"[LivingMemoryBridge] HTTP 桥服务已成功启动: http://{self.host}:{self.port} (state={self.state}, instance={self.bridge_instance_id})"
            )
        except OSError as e:
            self.state = "port_conflict"
            self._release_lock()
            logger.error(f"[LivingMemoryBridge] 端口绑定冲突 (http://{self.host}:{self.port}): {e}")
        except Exception as e:
            self.state = "failed"
            self._release_lock()
            logger.error(
                f"[LivingMemoryBridge] 启动 HTTP 桥服务失败 (http://{self.host}:{self.port}): {e}",
                exc_info=True,
            )

    async def stop(self):
        """停止 HTTP 桥服务并释放单实例锁"""
        if self.state == "external_owner":
            self.state = "stopped"
            return
        if self._runner:
            try:
                await self._runner.cleanup()
                logger.info("[LivingMemoryBridge] HTTP 桥服务已停止")
            except Exception as e:
                logger.error(f"[LivingMemoryBridge] 停止服务异常: {e}")
            finally:
                self._runner = None
                self._site = None
        self._release_lock()
        self.state = "stopped"
