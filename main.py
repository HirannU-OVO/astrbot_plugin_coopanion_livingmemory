"""main.py - LivingMemory 桌宠直连桥接插件
负责插件生命周期管理与 HTTP 服务托管
"""

from __future__ import annotations

import asyncio
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

        # 启动后台服务任务
        self._start_server_task()

    def _start_server_task(self):
        async def _run():
            try:
                await self.server.start()
            except Exception as e:
                logger.error(f"[LivingMemoryBridge] 服务启动异常: {e}", exc_info=True)

        self._bg_task = asyncio.create_task(_run())

    async def terminate(self):
        """插件卸载或 AstrBot 退出时的清理逻辑"""
        logger.info("[LivingMemoryBridge] 插件正在卸载，关闭 HTTP 服务...")
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        if self.server:
            await self.server.stop()

    @filter.command("lmbridge")
    async def cmd_lmbridge(self, event: AstrMessageEvent):
        """查询 LivingMemory 桥接状态"""
        engine = self.server._get_memory_engine()
        connected = engine is not None

        stats_text = "未连接"
        if connected:
            try:
                stats = await engine.get_statistics()
                stats_text = f"已连接 (已存储记忆数: {stats.get('total_memories', '未知')})"
            except Exception as e:
                stats_text = f"已连接 (查询统计失败: {e})"

        msg = (
            f"=== LivingMemory 桥接状态 ===\n"
            f"端口: {self.server.host}:{self.server.port}\n"
            f"引擎状态: {stats_text}\n"
            f"默认会话: {self.server.default_session_id}\n"
            f"默认人格: {self.server.default_persona_id}"
        )
        yield event.plain_result(msg)
