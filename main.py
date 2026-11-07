"""main.py - LivingMemory 桌宠直连桥接插件
负责插件生命周期管理与 HTTP 服务托管
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .bridge_server import LivingMemoryBridgeServer


class LivingMemoryBridgePlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # 实例化 HTTP 桥接服务器
        self.server = LivingMemoryBridgeServer(context, self.config)
        self._bg_task: asyncio.Task | None = None
        self._bg_init_task: asyncio.Task | None = None

        # 同名副本放进 plugins/ 会让面板保存/热重载指向错对象，启动时先查一遍
        self._warn_duplicate_plugin_dirs()

        # 启动后台服务任务
        self._start_server_task()

    def _warn_duplicate_plugin_dirs(self) -> None:
        own_name = "astrbot_plugin_coopanion_livingmemory"
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

            plugin_root = Path(get_astrbot_plugin_path())
            duplicates: list[str] = []
            for child in plugin_root.iterdir():
                if not child.is_dir() or child.name == own_name:
                    continue
                metadata = child / "metadata.yaml"
                if not metadata.is_file():
                    continue
                try:
                    text = metadata.read_text(encoding="utf-8-sig", errors="ignore")[:2000]
                except OSError:
                    continue
                match = re.search(r"^name:\s*([^\s#]+)", text, re.M)
                if match and match.group(1).strip() == own_name:
                    duplicates.append(child.name)
            if duplicates:
                logger.warning(
                    "[LivingMemoryBridge] 检测到同名插件目录 "
                    f"{duplicates}；面板保存/热重载可能指向副本，请把副本移出 data/plugins/"
                )
        except Exception as e:
            logger.debug(f"[LivingMemoryBridge] 同名插件目录检查失败: {e}")

    def _start_server_task(self):
        async def _run():
            try:
                await self.server.start()
                # 服务起来后在后台做一次识别，结果直接写回插件配置，面板即可确认/修改
                self._bg_init_task = asyncio.create_task(self._auto_init())
            except Exception as e:
                logger.error(f"[LivingMemoryBridge] 服务启动异常: {e}", exc_info=True)

        self._bg_task = asyncio.create_task(_run())

    async def _auto_init(self):
        """启动后主动做一次自动识别。"""
        ready = False
        for _ in range(30):
            try:
                await self.server._ensure_auto()
                if self.server._auto_ready:
                    ready = True
                    logger.info(
                        "[LivingMemoryBridge] 启动期自动识别完成，识别结果已写入插件配置"
                    )
                    break
            except Exception as e:
                logger.debug(f"[LivingMemoryBridge] 启动期自动识别暂未完成: {e}")
            await asyncio.sleep(2)
        if not ready:
            logger.warning(
                "[LivingMemoryBridge] 启动期自动识别未完成，后续请求会继续重试；"
                "可在面板手动填写会话/人格/身份"
            )

    async def terminate(self):
        """插件卸载或 AstrBot 退出时的清理逻辑"""
        logger.info("[LivingMemoryBridge] 插件正在卸载，关闭 HTTP 服务...")
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        if self._bg_init_task and not self._bg_init_task.done():
            self._bg_init_task.cancel()
        if self.server:
            await self.server.stop()

    @filter.command("lmbridge")
    async def cmd_lmbridge(
        self,
        event: AstrMessageEvent,
        action: str = "",
        old: str = "",
        new: str = "",
        mode: str = "",
    ):
        """查询桥接状态；/lmbridge migrate-persona <旧人格> <新人格> [apply]"""
        if action.casefold() in ("migrate-persona", "migrate_persona"):
            if not old or not new:
                yield event.plain_result(
                    "用法：/lmbridge migrate-persona <旧人格> <新人格> [apply]\n"
                    "默认只预览；加 apply 才真正迁移 documents / graph_entries / memory_atoms。"
                )
                return
            dry_run = mode.casefold() not in ("apply", "yes", "confirm")
            result = await self.server.migrate_persona(old, new, dry_run=dry_run)
            yield event.plain_result(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
            return

        engine = self.server._get_memory_engine()
        connected = engine is not None

        stats_text = "未连接"
        if connected:
            try:
                stats = await engine.get_statistics()
                stats_text = f"已连接 (已存储记忆数: {stats.get('total_memories', '未知')})"
            except Exception as e:
                stats_text = f"已连接 (查询统计失败: {e})"

        mode_name = (
            "桌宠单独记"
            if self.server.memory_share_mode == "pet_only"
            else "一起记"
        )
        filtering = "开启" if self.server._persona_filtering_enabled() else "关闭"
        msg = (
            f"=== LivingMemory 桥接状态 ===\n"
            f"端口: {self.server.host}:{self.server.port}\n"
            f"引擎状态: {stats_text}\n"
            f"默认会话: {self.server.default_session_id}\n"
            f"默认人格: {self.server.default_persona_id}\n"
            f"记忆共享模式: {mode_name}\n"
            f"桌宠人格: {self.server._effective_pet_persona()}\n"
            f"上游按人格过滤: {filtering}"
        )
        adapter_instance = self.server._session_adapter_id()
        platform_type = self.server._platform_label(
            self.server._session_platform(self.server.default_session_id)
        )
        msg += (
            f"\n适配器实例名: {adapter_instance or '未识别'}"
            f"\n平台类型: {platform_type}"
        )
        try:
            overview = await self.server.get_session_overview()
            sessions = overview.get("sessions") or []
            if sessions:
                msg += "\n\n检测到的会话:"
                for row in sessions:
                    msg += (
                        f"\n- {row['session_id']} | 平台: {row['platform_label']}"
                        f" | 人格: {row['persona_id'] or '未识别'}"
                        f" | 消息: {row['message_count']}"
                    )
            else:
                msg += "\n\n检测到的会话: 无"
        except Exception as e:
            msg += f"\n\n检测到的会话: 查询失败（{e}）"
        if self.server._identity_warning:
            msg += f"\n身份告警: {self.server._identity_warning}"
        if self.server._persona_warning:
            msg += f"\n人格告警: {self.server._persona_warning}"
        yield event.plain_result(msg)
