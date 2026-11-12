"""上游 LivingMemory 数据适配层。

业务层只通过本模块访问上游 LivingMemory 的会话历史、记忆数据和反思组件。
本模块集中处理上游插件的私有属性探测、SQLite 表结构兼容和能力降级。

能力按首次真正使用时探测，并在进程内缓存；探测失败只关闭对应能力，
不会让整个桥接服务起不来。所有降级都只记录一次 warning，并通过
capability_summary()/capability_report() 暴露给面板和 /lmbridge doctor。

替换点：
- 公开查询：conversation_counts / session_personas / bot_identity /
  dominant_persona / detect_identities 仍依赖 conversations.db 与
  livingmemory.db 的只读表结构。上游提供公开查询接口后，优先替换这几个
  方法的实现，业务层无需改动。
- 公开组件：engine / conversation_api / reflection_component / config_get
  仍按上游插件现有属性发现。上游提供稳定 getter 后，替换这些发现方法即可。
- 人格迁移：migrate_persona 仍依赖记忆引擎 db_connection；默认 dry-run，
  只有显式 apply 才写入。上游提供公开迁移接口后替换此方法。
"""

from __future__ import annotations

import inspect
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools


@dataclass(frozen=True)
class UpstreamCapability:
    key: str
    label: str
    enabled: bool
    reason: str = ""
    # pending=True 表示上游尚未初始化完成，应稍后重试；
    # pending=False 且 enabled=False 才是真正的不可用/不兼容。
    pending: bool = False


class UpstreamLivingMemoryAdapter:
    """对上游 LivingMemory 私有数据的最小访问层。"""

    UPSTREAM_PLUGIN = "astrbot_plugin_livingmemory"

    CAPABILITY_LABELS = {
        "upstream_plugin": "上游插件",
        "engine": "记忆引擎",
        "conversation_api": "对话管理",
        "reflection": "记忆反思",
        "conversations_db": "会话数据库",
        "livingmemory_db": "记忆数据库",
        "persona_migration": "人格迁移",
        "memory_processor": "记忆处理器",
    }
    _CONVERSATION_SPEC = {
        "messages": ("id", "session_id", "role", "sender_id", "sender_name", "metadata")
    }
    _LIVINGMEMORY_SPEC = {
        "documents": ("id", "metadata"),
        "graph_entries": ("persona_id", "metadata"),
        "memory_atoms": ("persona_id",),
    }
    _DOCUMENTS_SPEC = {"documents": ("id", "metadata")}

    def __init__(self, context: Any | None = None):
        self.context = context
        self._capabilities: dict[str, UpstreamCapability] = {}
        self._checked_at: dict[str, float] = {}
        self._warned: set[str] = set()
        self._version: str | None = None
        # 负缓存 TTL：上游初始化完成前会先探测失败，不能永久缓存。
        self._negative_ttl = 2.0

    # ------------------------------------------------------------------
    # 能力探测与缓存
    # ------------------------------------------------------------------
    def _record(
        self,
        key: str,
        enabled: bool,
        reason: str = "",
        pending: bool = False,
    ) -> UpstreamCapability:
        capability = UpstreamCapability(
            key=key,
            label=self.CAPABILITY_LABELS.get(key, key),
            enabled=enabled,
            reason="" if enabled else reason,
            pending=bool(pending and not enabled),
        )
        self._capabilities[key] = capability
        self._checked_at[key] = time.monotonic()
        if enabled:
            if key in self._warned:
                self._warned.discard(key)
                logger.info(
                    f"[LivingMemoryBridge] 上游能力「{capability.label}」已恢复"
                )
            return capability
        if key not in self._warned:
            self._warned.add(key)
            if capability.pending:
                logger.info(
                    f"[LivingMemoryBridge] 上游能力「{capability.label}」尚未就绪，"
                    "桥接会自动重试"
                )
            else:
                logger.warning(
                    f"[LivingMemoryBridge] 上游能力「{capability.label}」不可用："
                    f"{reason}（上游版本 {self.upstream_version()}）"
                )
        return capability

    def refresh(self) -> None:
        """清空能力缓存，让下一次探测重新读取上游状态。

        用于健康检查、/lmbridge doctor 和启动自检重试；不重置版本/配置。
        """
        self._capabilities.clear()
        self._checked_at.clear()
        self._warned.clear()

    def _upstream_initialized(self, plugin: Any | None) -> bool:
        if plugin is None:
            return False
        initializer = getattr(plugin, "initializer", None)
        return bool(getattr(initializer, "is_initialized", False))

    def _not_ready_reason(self, plugin: Any | None, fallback: str) -> tuple[str, bool]:
        if plugin is None:
            return "未找到上游 LivingMemory 插件实例", True
        if not self._upstream_initialized(plugin):
            return "上游 LivingMemory 尚未初始化完成", True
        return fallback, False

    def capability(self, key: str) -> UpstreamCapability:
        cached = self._capabilities.get(key)
        if cached is not None:
            if cached.enabled:
                return cached
            # 负缓存不能永久生效：上游初始化完成或接口热更新后要重新探测。
            age = time.monotonic() - self._checked_at.get(key, 0.0)
            if age < self._negative_ttl:
                return cached
        return self._probe_capability(key)

    def _probe_capability(self, key: str) -> UpstreamCapability:
        if key == "upstream_plugin":
            plugin = self.livingmemory_plugin()
            return self._record(
                key, plugin is not None, "未找到上游 LivingMemory 插件实例",
                pending=plugin is None,
            )
        if key == "engine":
            plugin = self.livingmemory_plugin()
            reason, pending = self._not_ready_reason(
                plugin, "未找到可用的记忆引擎接口"
            )
            if plugin is None:
                return self._record(key, False, reason, pending=pending)
            engine = self._probe_member(
                plugin, ("memory_engine", "engine", "memory_service"), "add_memory"
            )
            if engine is None:
                return self._record(key, False, reason, pending=pending)
            return self._record(key, True)
        if key == "conversation_api":
            plugin = self.livingmemory_plugin()
            reason, pending = self._not_ready_reason(
                plugin, "未找到可用的对话管理接口"
            )
            if plugin is None:
                return self._record(key, False, reason, pending=pending)
            manager = self._probe_member(
                plugin, ("conversation_manager", "conv_manager"), "add_message"
            )
            if manager is None:
                return self._record(key, False, reason, pending=pending)
            return self._record(key, True)
        if key == "reflection":
            plugin = self.livingmemory_plugin()
            reason, pending = self._not_ready_reason(
                plugin, "未找到可用的记忆反思组件"
            )
            component = self._probe_reflection()
            return self._record(
                key, component is not None, reason, pending=pending
            )
        if key == "conversations_db":
            ok, reason = self._check_sqlite(
                "conversations.db", self._CONVERSATION_SPEC
            )
            plugin = self.livingmemory_plugin()
            pending = not ok and not self._upstream_initialized(plugin)
            return self._record(key, ok, reason, pending=pending)
        if key == "livingmemory_db":
            ok, reason = self._check_sqlite(
                "livingmemory.db", self._DOCUMENTS_SPEC
            )
            plugin = self.livingmemory_plugin()
            pending = not ok and not self._upstream_initialized(plugin)
            return self._record(key, ok, reason, pending=pending)
        if key == "persona_migration":
            engine = self.capability("engine")
            if not engine.enabled:
                return self._record(
                    key, False, "记忆引擎不可用", pending=engine.pending
                )
            ok, reason = self._check_sqlite(
                "livingmemory.db", self._LIVINGMEMORY_SPEC
            )
            return self._record(key, ok, reason)
        if key == "memory_processor":
            plugin = self.livingmemory_plugin()
            reason, pending = self._not_ready_reason(
                plugin, "未找到可用的记忆处理器接口"
            )
            if plugin is None:
                return self._record(key, False, reason, pending=pending)
            processor = self._probe_member(
                plugin,
                ("memory_processor", "processor"),
                "build_memory_from_structured_data",
            )
            if processor is None:
                return self._record(key, False, reason, pending=pending)
            return self._record(key, True)
        return self._record(key, False, "未知能力")

    def _probe_reflection(self) -> Any | None:
        plugin = self.livingmemory_plugin()
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

    def capability_rows(self) -> list[UpstreamCapability]:
        return [self.capability(key) for key in self.CAPABILITY_LABELS]

    def capability_summary(self) -> dict[str, Any]:
        rows = self.capability_rows()
        disabled = [row for row in rows if not row.enabled]
        pending = [row for row in disabled if row.pending]
        version = self.upstream_version()
        warning = ""
        if pending:
            names = "、".join(row.label for row in pending)
            warning = (
                "上游 LivingMemory 仍在初始化，桥接会自动重试；"
                f"暂挂起：{names}"
            )
        elif disabled:
            names = "、".join(row.label for row in disabled)
            warning = f"上游能力不可用：{names}"
        return {
            "upstream_version": version,
            "ok": not disabled,
            "warning": warning,
            "capabilities": {
                row.key: {
                    "label": row.label,
                    "enabled": row.enabled,
                    "pending": row.pending,
                    "reason": row.reason,
                }
                for row in rows
            },
        }

    def capability_report(self) -> str:
        rows = self.capability_rows()
        disabled = [row for row in rows if not row.enabled]
        lines = [
            "=== LivingMemory 上游能力 ===",
            f"上游版本：{self.upstream_version()}",
            "总体状态：" + ("正常" if not disabled else "降级"),
        ]
        for row in rows:
            if row.enabled:
                state = "enabled"
            elif row.pending:
                state = "pending"
            else:
                state = "disabled"
            suffix = f"（{row.reason}）" if row.reason else ""
            lines.append(f"- {row.label}: {state}{suffix}")
        pending = [row for row in disabled if row.pending]
        if pending:
            lines.append(
                "提示："
                + "、".join(row.label for row in pending)
                + " 等待上游初始化；桥接会自动重试，勿据此判断版本不兼容。"
            )
        elif disabled:
            lines.append(
                "提示："
                + "、".join(row.label for row in disabled)
                + " 不可用；其余能力继续工作。"
            )
        return "\n".join(lines)

    def write_block_reason(self, kind: str) -> str | None:
        """写入前检查所需能力；返回第一个不可用原因，None 表示可写。"""
        requirements = {
            "search": ("engine", "livingmemory_db"),
            "memory": ("engine", "livingmemory_db"),
            "chat": (
                "conversation_api",
                "conversations_db",
                "engine",
                "livingmemory_db",
                "reflection",
            ),
            "reflect": (
                "conversation_api",
                "engine",
                "livingmemory_db",
                "reflection",
            ),
        }
        for key in requirements.get(kind, ()):
            cap = self.capability(key)
            if not cap.enabled:
                return f"{cap.label}不可用：{cap.reason}"
        return None

    # ------------------------------------------------------------------
    # 上游组件发现：私有属性探测只允许出现在本模块
    # ------------------------------------------------------------------
    def livingmemory_plugin(self) -> Any | None:
        if self.context is None:
            return None
        try:
            star_meta = self.context.get_registered_star(self.UPSTREAM_PLUGIN)
            if star_meta is None:
                return None
            for attr in ("star_cls", "star", "inst", "instance", "plugin"):
                candidate = getattr(star_meta, attr, None)
                if candidate is not None and hasattr(candidate, "initializer"):
                    return candidate
            if hasattr(star_meta, "initializer"):
                return star_meta
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 获取上游插件对象失败: {e}")
        return None

    @staticmethod
    def _probe_member(
        plugin: Any, names: tuple[str, ...], required_method: str
    ) -> Any | None:
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

    def engine(self) -> Any | None:
        if not self.capability("engine").enabled:
            return None
        return self._probe_member(
            self.livingmemory_plugin(),
            ("memory_engine", "engine", "memory_service"),
            "add_memory",
        )

    def conversation_api(self) -> Any | None:
        if not self.capability("conversation_api").enabled:
            return None
        return self._probe_member(
            self.livingmemory_plugin(),
            ("conversation_manager", "conv_manager"),
            "add_message",
        )

    def reflection_component(self) -> Any | None:
        if not self.capability("reflection").enabled:
            return None
        return self._probe_reflection()

    def memory_processor(self) -> Any | None:
        if not self.capability("memory_processor").enabled:
            return None
        return self._probe_member(
            self.livingmemory_plugin(),
            ("memory_processor", "processor"),
            "build_memory_from_structured_data",
        )

    # ------------------------------------------------------------------
    # 工具可用性与启用状态判定 (对齐 A-02 ~ A-07 / LM-E2E-01 ~ LM-E2E-07)
    # ------------------------------------------------------------------
    def is_search_enabled(self) -> tuple[bool, str]:
        engine_cap = self.capability("engine")
        if not engine_cap.enabled:
            return False, f"记忆引擎不可用: {engine_cap.reason or '未就绪'}"
        return True, ""

    def is_record_enabled(self) -> tuple[bool, str]:
        engine_cap = self.capability("engine")
        if not engine_cap.enabled:
            return False, f"记忆引擎不可用: {engine_cap.reason or '未就绪'}"
        return True, ""

    def is_memorize_enabled(self) -> tuple[bool, str]:
        engine_cap = self.capability("engine")
        if not engine_cap.enabled:
            return False, f"记忆引擎不可用: {engine_cap.reason or '未就绪'}"
        proc_cap = self.capability("memory_processor")
        if not proc_cap.enabled:
            return False, f"记忆处理器不可用: {proc_cap.reason or '未就绪'}"
        if not bool(self.config_get("agent_tools.enable_memorize_tool", False)):
            return False, "上游配置 agent_tools.enable_memorize_tool 未开启"
        return True, ""

    def is_recall_enabled(self) -> tuple[bool, str]:
        engine_cap = self.capability("engine")
        if not engine_cap.enabled:
            return False, f"记忆引擎不可用: {engine_cap.reason or '未就绪'}"
        if not bool(self.config_get("agent_tools.enable_recall_tool", True)):
            return False, "上游配置 agent_tools.enable_recall_tool 未开启"
        return True, ""

    def is_agentic_recall_enabled(self) -> tuple[bool, str]:
        ok, reason = self.is_recall_enabled()
        if not ok:
            return False, reason
        if not bool(self.config_get("agent_tools.enable_agentic_recall_beta", False)):
            return False, "上游配置 agent_tools.enable_agentic_recall_beta 未开启"
        return True, ""

    def is_evidence_enabled(self) -> tuple[bool, str]:
        return self.is_agentic_recall_enabled()

    def is_forget_enabled(self) -> tuple[bool, str]:
        engine_cap = self.capability("engine")
        if not engine_cap.enabled:
            return False, f"记忆引擎不可用: {engine_cap.reason or '未就绪'}"
        if not bool(self.config_get("agent_tools.enable_forget_tool", False)):
            return False, "上游配置 agent_tools.enable_forget_tool 未开启"
        return True, ""

    def is_status_enabled(self) -> tuple[bool, str]:
        return True, ""

    def get_tool_availability(self, tool_name: str) -> tuple[bool, str]:
        checkers = {
            "mem_search": self.is_search_enabled,
            "mem_record": self.is_record_enabled,
            "mem_memorize": self.is_memorize_enabled,
            "mem_recall": self.is_recall_enabled,
            "mem_evidence": self.is_evidence_enabled,
            "mem_forget": self.is_forget_enabled,
            "mem_status": self.is_status_enabled,
        }
        checker = checkers.get(tool_name)
        if not checker:
            return False, f"未知工具: {tool_name}"
        return checker()

    def config_get(self, key: str, default: Any) -> Any:
        plugin = self.livingmemory_plugin()
        for holder in (
            plugin,
            getattr(plugin, "initializer", None) if plugin else None,
        ):
            cm = getattr(holder, "config_manager", None) if holder else None
            if cm is not None and hasattr(cm, "get"):
                try:
                    return cm.get(key, default)
                except Exception as e:
                    logger.warning(f"[LivingMemoryBridge] 读取上游配置失败: {e}")
                    continue
        return default

    def upstream_version(self) -> str:
        if self._version is not None:
            return self._version
        try:
            plugin = self.livingmemory_plugin()
            if plugin is not None:
                for attr in ("version", "plugin_version"):
                    value = getattr(plugin, attr, None)
                    if value:
                        self._version = str(value)
                        return self._version
            candidates = []
            if plugin is not None:
                module = inspect.getmodule(plugin.__class__)
                module_file = getattr(module, "__file__", None) if module else None
                if module_file:
                    candidates.append(os.path.dirname(module_file))
            data_dir = self._data_dir()
            candidates.append(
                os.path.join(
                    os.path.dirname(os.path.dirname(data_dir)),
                    "plugins",
                    self.UPSTREAM_PLUGIN,
                )
            )
            for directory in candidates:
                metadata = os.path.join(directory, "metadata.yaml")
                if not os.path.exists(metadata):
                    continue
                with open(metadata, encoding="utf-8") as f:
                    for line in f:
                        match = re.match(
                            r"^\s*version\s*:\s*['\"]?([^'\"\s]+)", line
                        )
                        if match:
                            self._version = match.group(1)
                            return self._version
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 读取上游版本失败: {e}")
        self._version = "未知"
        return self._version

    # ------------------------------------------------------------------
    # 表结构探测：只返回能力和原因，不抛异常
    # ------------------------------------------------------------------
    def _data_dir(self) -> str:
        return str(StarTools.get_data_dir(self.UPSTREAM_PLUGIN))

    @staticmethod
    def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}

    def _check_sqlite(
        self, filename: str, spec: dict[str, tuple[str, ...]]
    ) -> tuple[bool, str]:
        try:
            db = os.path.join(self._data_dir(), filename)
        except Exception as e:
            return False, f"无法定位数据目录：{e}"
        if not os.path.exists(db):
            return False, f"数据库文件不存在：{filename}"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        except Exception as e:
            return False, f"无法打开 {filename}：{e}"
        try:
            tables = {
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            problems: list[str] = []
            for table, required_columns in spec.items():
                if table not in tables:
                    problems.append(f"缺少表 {table}")
                    continue
                columns = self._table_columns(con, table)
                missing = [col for col in required_columns if col not in columns]
                if missing:
                    problems.append(f"{table} 缺少列 {', '.join(missing)}")
            if problems:
                return False, f"{filename}：" + "；".join(problems)
            return True, ""
        except Exception as e:
            return False, f"读取 {filename} 结构失败：{e}"
        finally:
            con.close()

    def _connect_readonly(
        self, filename: str, capability_key: str
    ) -> sqlite3.Connection | None:
        if not self.capability(capability_key).enabled:
            return None
        try:
            db = os.path.join(self._data_dir(), filename)
            return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        except Exception as e:
            logger.warning(f"[LivingMemoryBridge] 打开 {filename} 失败：{e}")
            return None

    # ------------------------------------------------------------------
    # 只读查询：能力不可用时返回空结果，不抛异常
    # ------------------------------------------------------------------
    def conversation_counts(self) -> dict[str, int]:
        con = self._connect_readonly("conversations.db", "conversations_db")
        if con is None:
            return {}
        out: dict[str, int] = {}
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
        return out

    def session_personas(self) -> dict[str, str]:
        con = self._connect_readonly("livingmemory.db", "livingmemory_db")
        if con is None:
            return {}
        out: dict[str, str] = {}
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
        return out

    def bot_identity(self, session_id: str, fallback: tuple[str, str]) -> tuple[str, str]:
        con = self._connect_readonly("conversations.db", "conversations_db")
        if con is None:
            return fallback
        try:
            row = con.execute(
                "SELECT sender_id, sender_name FROM messages "
                "WHERE session_id=? AND sender_id IS NOT NULL AND sender_id<>'' "
                "AND (role='assistant' "
                "OR COALESCE(json_extract(metadata,'$.is_bot_message'),0)=1) "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            con.close()
        if row:
            bid = str(row[0] or "").strip()
            bname = str(row[1] or "").strip() or bid
            if bid:
                return (bid, bname)
        return fallback

    def dominant_persona(self, session_id: str) -> str | None:
        con = self._connect_readonly("livingmemory.db", "livingmemory_db")
        if con is None:
            return None
        try:
            row = con.execute(
                "SELECT json_extract(metadata,'$.persona_id') AS p, COUNT(*) AS c "
                "FROM documents WHERE json_extract(metadata,'$.session_id')=? "
                "AND p IS NOT NULL AND p <> '' GROUP BY p ORDER BY c DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        finally:
            con.close()
        if row and row[0]:
            return str(row[0])
        return None

    def detect_identities(
        self, current_user_id: str = ""
    ) -> tuple[str, str, str, str] | None:
        bot = user = None
        con = self._connect_readonly("conversations.db", "conversations_db")
        if con is not None:
            try:
                rows = con.execute(
                    "SELECT sender_id, sender_name, role, "
                    "COALESCE(json_extract(metadata,'$.is_bot_message'),0), "
                    "COALESCE(json_extract(metadata,'$.source'),'') "
                    "FROM messages WHERE sender_id IS NOT NULL AND sender_id <> '' "
                    "ORDER BY id DESC LIMIT 500"
                ).fetchall()
            finally:
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

        fallback_bot, fallback_user = self._document_participants(current_user_id)
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

    def _document_participants(
        self, current_user_id: str
    ) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
        con = self._connect_readonly("livingmemory.db", "livingmemory_db")
        if con is None:
            return (None, None)
        try:
            rows = con.execute(
                "SELECT json_extract(je.value,'$.identity_key'), "
                "json_extract(je.value,'$.display_name'), "
                "json_extract(je.value,'$.is_bot'), COUNT(*) AS c "
                "FROM documents d, json_each(d.metadata,'$.participant_identities') je "
                "GROUP BY 1,2,3 ORDER BY c DESC"
            ).fetchall()
        finally:
            con.close()
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
                if not current_user_id or id_part == current_user_id:
                    if name_s and not name_s.isdigit():
                        user = (id_part, name_s)
                    elif current_user_id:
                        user = (id_part, current_user_id)
            if bot and user:
                break
        if user is None and current_user_id:
            user = (current_user_id, current_user_id)
        return (bot, user)

    async def migrate_persona(
        self, engine: Any, old_persona: str, new_persona: str, dry_run: bool = True
    ) -> dict[str, Any]:
        cap = self.capability("persona_migration")
        if not cap.enabled:
            return {
                "ok": False,
                "error": f"人格迁移暂不可用：{cap.reason}",
            }
        old = str(old_persona or "").strip()
        new = str(new_persona or "").strip()
        if not old or not new:
            return {"ok": False, "error": "需要提供旧人格和新人格"}
        if old == new:
            return {"ok": False, "error": "新旧人格相同"}
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
