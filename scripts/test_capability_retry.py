"""上游能力探测：启动期 pending 不能在初始化后被永久缓存。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from upstream_adapter import UpstreamLivingMemoryAdapter


class FakeEngine:
    async def add_memory(self, *args, **kwargs):
        return "mem"


class FakeConversation:
    async def add_message(self, *args, **kwargs):
        return None


class FakeReflection:
    def _build_reflection_plan_for_cursor(self, *args, **kwargs):
        return None


class FakeInitializer:
    is_initialized = False
    memory_engine = None
    conversation_manager = None


class FakeEventHandler:
    _memory_reflection = None


class FakePlugin:
    def __init__(self):
        self.initializer = FakeInitializer()
        self.event_handler = FakeEventHandler()


class FakeStarMeta:
    def __init__(self, plugin):
        # 与 AstrBot StarMetadata 一致：真实插件实例挂在 star_cls。
        self.star_cls = plugin


class FakeContext:
    def __init__(self, plugin):
        self._plugin = plugin

    def get_registered_star(self, name):
        return FakeStarMeta(self._plugin)


def main():
    plugin = FakePlugin()
    adapter = UpstreamLivingMemoryAdapter(FakeContext(plugin))
    adapter._negative_ttl = 0.01

    engine = adapter.capability("engine")
    assert not engine.enabled and engine.pending, engine

    plugin.initializer.is_initialized = True
    plugin.initializer.memory_engine = FakeEngine()
    plugin.initializer.conversation_manager = FakeConversation()
    plugin.event_handler._memory_reflection = FakeReflection()
    # 模拟负缓存 TTL 已过期（生产为 2s，测试直接改时间戳）
    adapter._checked_at["engine"] = 0.0
    adapter._checked_at["conversation_api"] = 0.0
    adapter._checked_at["reflection"] = 0.0

    engine = adapter.capability("engine")
    conversation = adapter.capability("conversation_api")
    reflection = adapter.capability("reflection")

    assert engine.enabled, engine
    assert conversation.enabled, conversation
    assert reflection.enabled, reflection
    summary = adapter.capability_summary()
    for key in ("engine", "conversation_api", "reflection"):
        assert summary["capabilities"][key]["enabled"], summary
    assert "初始化" not in summary["warning"], summary

    print("PASS: pending -> ready after upstream initialization")
    print(summary["capabilities"])


if __name__ == "__main__":
    main()
