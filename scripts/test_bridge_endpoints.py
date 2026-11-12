"""test_bridge_endpoints.py - 自动化验证桥接端点、严格参数校验、清单与单实例状态机"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contract import (
    SCHEMA_VERSION,
    TOOLS_SCHEMA_VERSION,
    TOOLS_DEF,
    validate_tool_params,
)
from upstream_adapter import UpstreamLivingMemoryAdapter
from bridge_server import LivingMemoryBridgeServer, VERSION


class FakeEngine:
    async def get_statistics(self):
        return {"total_memories": 42}

    async def add_memory(self, content, session_id, persona_id, importance, metadata):
        return 1001

    async def search_memories(self, query, k, session_id, persona_id):
        class FakeDoc:
            doc_id = 1001
            content = f"recall result for {query}"
            final_score = 0.95
            metadata = {"importance": 0.8, "session_id": session_id, "persona_id": persona_id}
        return [FakeDoc()]

    async def archive_memories(self, memory_ids, audit=None):
        return len(memory_ids)


class FakeProcessor:
    def build_memory_from_structured_data(self, structured_data, is_group_chat=False, fallback_excerpt=""):
        return f"Processed: {structured_data['summary']}", {"topics": structured_data.get("topics", [])}, 0.75


class FakeConfigManager:
    def __init__(self, data=None):
        self._data = data or {
            "agent_tools.enable_recall_tool": True,
            "agent_tools.enable_agentic_recall_beta": False,
            "agent_tools.enable_memorize_tool": True,
            "agent_tools.enable_forget_tool": False,
        }

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeInitializer:
    is_initialized = True
    def __init__(self):
        self.memory_engine = FakeEngine()
        self.memory_processor = FakeProcessor()
        self.config_manager = FakeConfigManager()


class FakePlugin:
    def __init__(self):
        self.initializer = FakeInitializer()
        self.version = "2.10.0"


class FakeStarMeta:
    def __init__(self, plugin):
        self.star_cls = plugin


class FakeContext:
    def __init__(self, plugin):
        self._plugin = plugin

    def get_registered_star(self, name):
        return FakeStarMeta(self._plugin)


async def run_tests():
    print("=== 1. 契约参数校验 (LM-E2E-18 ~ 21) ===")
    # LM-E2E-18: 未知字段 target_user_id
    ok, err = validate_tool_params("mem_search", {"query": "hello", "target_user_id": "123"})
    assert not ok and err["code"] == "unknown_field" and err["field"] == "target_user_id", err
    print("PASS: LM-E2E-18 未知字段被严格拒绝 (422 unknown_field)")

    # LM-E2E-19: 缺 query
    ok, err = validate_tool_params("mem_search", {})
    assert not ok and err["code"] == "missing_field" and err["field"] == "query", err
    print("PASS: LM-E2E-19 缺失必填字段被拒绝 (422 missing_field)")

    # LM-E2E-20: 错误类型 topics 传字符串
    ok, err = validate_tool_params("mem_record", {"content": "test", "topics": "not-a-list"})
    assert not ok and err["code"] == "invalid_type" and err["field"] == "topics", err
    print("PASS: LM-E2E-20 错误类型被拒绝 (422 invalid_type)")

    # 有效参数
    ok, err = validate_tool_params("mem_search", {"query": "test query", "limit": 3})
    assert ok and err is None
    ok, err = validate_tool_params("mem_memorize", {
        "content": "test content",
        "topics": ["work"],
        "key_facts": ["fact1"],
        "sentiment": "positive",
        "importance": 0.8,
        "reason": "important note"
    })
    assert ok and err is None
    print("PASS: 合法参数校验全通过")

    print("\n=== 2. 上游可用性与工具状态判定 (LM-E2E-03, LM-E2E-04) ===")
    plugin = FakePlugin()
    adapter = UpstreamLivingMemoryAdapter(FakeContext(plugin))
    adapter._negative_ttl = 0.01

    assert adapter.is_search_enabled()[0] is True
    assert adapter.is_record_enabled()[0] is True
    assert adapter.is_memorize_enabled()[0] is True
    assert adapter.is_recall_enabled()[0] is True
    # Beta 未开
    assert adapter.is_evidence_enabled()[0] is False
    assert "agent_tools.enable_agentic_recall_beta" in adapter.is_evidence_enabled()[1]
    # Forget 未开
    assert adapter.is_forget_enabled()[0] is False
    assert "agent_tools.enable_forget_tool" in adapter.is_forget_enabled()[1]
    print("PASS: LM-E2E-03 & 04 工具开启与关闭状态精准识别")

    print("\n=== 3. Server 初始化与 Manifest 生成 (A-02, LM-E2E-01 ~ 07) ===")
    server = LivingMemoryBridgeServer(FakeContext(plugin), {"port": 16198, "host": "127.0.0.1"})
    server.upstream = adapter

    class DummyRequest:
        pass

    manifest_resp = await server.handle_manifest(DummyRequest())
    assert manifest_resp.status == 200
    import json
    manifest_data = json.loads(manifest_resp.text)
    assert manifest_data["schema_version"] == SCHEMA_VERSION
    assert manifest_data["tools_schema_version"] == TOOLS_SCHEMA_VERSION
    assert len(manifest_data["catalog"]) == 7

    active_names = [t["name"] for t in manifest_data["tools"]]
    assert "mem_search" in active_names
    assert "mem_record" in active_names
    assert "mem_memorize" in active_names
    assert "mem_recall" in active_names
    assert "mem_status" in active_names
    assert "mem_evidence" not in active_names
    assert "mem_forget" not in active_names
    print(f"PASS: Manifest 生成正确，可用工具: {active_names} (关闭的工具仅在 catalog)")

    print("\n=== 4. 记忆写入与深度沉淀 (A-03, LM-E2E-08 ~ 09) ===")
    class MockJsonRequest:
        def __init__(self, payload):
            self._payload = payload
        async def json(self):
            return self._payload

    # 测试 handle_memorize
    mem_req = MockJsonRequest({
        "content": "羽羽答应周末去钓鱼",
        "topics": ["钓鱼", "周末"],
        "key_facts": ["时间是周六"],
        "sentiment": "positive",
        "importance": 0.85,
        "reason": "重要约定",
    })
    mem_resp = await server.handle_memorize(mem_req)
    assert mem_resp.status == 200
    mem_data = json.loads(mem_resp.text)
    assert mem_data["ok"] is True
    assert mem_data["status"] == "memorized"
    assert mem_data["memory_id"] == 1001
    print("PASS: handle_memorize 执行成功，返回 memory_id=1001")

    # 测试 handle_recall
    recall_req = MockJsonRequest({"query": "钓鱼", "limit": 2})
    recall_resp = await server.handle_recall(recall_req)
    assert recall_resp.status == 200
    recall_data = json.loads(recall_resp.text)
    assert recall_data["ok"] is True
    assert recall_data["count"] == 1
    assert recall_data["results"][0]["id"] == 1001
    print("PASS: handle_recall 执行成功，返回对应记忆")

    # 测试 handle_forget 在未启用时的明确拒绝 (LM-E2E-14 / LM-E2E-15)
    forget_req = MockJsonRequest({"memory_ids": [1001], "reason": "过期"})
    forget_resp = await server.handle_forget(forget_req)
    assert forget_resp.status == 403
    forget_data = json.loads(forget_resp.text)
    assert forget_data["code"] == "tool_disabled"
    print("PASS: handle_forget 在工具关闭时明确返回 403 tool_disabled")

    # 权限 fail-closed：开启 forget 工具后校验管理员白名单
    forget_plugin = FakePlugin()
    forget_plugin.initializer.config_manager._data["agent_tools.enable_forget_tool"] = True
    forget_server = LivingMemoryBridgeServer(
        FakeContext(forget_plugin), {"port": 16199, "host": "127.0.0.1"}
    )
    forget_server.upstream = UpstreamLivingMemoryAdapter(FakeContext(forget_plugin))

    # 身份来自会话 umo 解析，契约不允许请求体携带 user_id
    forget_server.default_session_id = "group:10000"

    # 未配置 admins_id：一律拒绝，不放行任何人
    no_admin_resp = await forget_server.handle_forget(
        MockJsonRequest({"memory_ids": [1001], "reason": "过期"})
    )
    assert no_admin_resp.status == 403
    assert json.loads(no_admin_resp.text)["code"] == "permission_denied"
    print("PASS: 未配置管理员时遗忘操作 fail-closed 拒绝")

    # 配置了白名单但请求者不在其中：拒绝
    forget_server.context.astrbot_config = {"admins_id": ["10000"]}
    forget_server.default_session_id = "group:20000"
    outsider_resp = await forget_server.handle_forget(
        MockJsonRequest({"memory_ids": [1001], "reason": "过期"})
    )
    assert outsider_resp.status == 403
    assert json.loads(outsider_resp.text)["code"] == "permission_denied"
    print("PASS: 非白名单请求者被拒绝")

    # 白名单内请求者：放行
    forget_server.default_session_id = "group:10000"
    admin_resp = await forget_server.handle_forget(
        MockJsonRequest({"memory_ids": [1001], "reason": "过期"})
    )
    assert admin_resp.status == 200, admin_resp.text
    assert json.loads(admin_resp.text)["ok"] is True
    print("PASS: 白名单管理员可正常归档记忆")

    print("\n=== 5. 单实例锁机制 (D-05, LM-E2E-22) ===")
    lock_ok = server._acquire_lock()
    assert lock_ok is True
    lock_file = server._lock_file_path()
    assert os.path.exists(lock_file)
    with open(lock_file, "r", encoding="utf-8") as f:
        lock_info = json.load(f)
    assert lock_info["pid"] == os.getpid()
    assert lock_info["bridge_instance_id"] == server.bridge_instance_id
    server._release_lock()
    assert not os.path.exists(lock_file)
    print("PASS: 锁文件成功写入与安全释放")

    print("\n===============================")
    print("ALL BRIDGE ENDPOINT TESTS PASSED")
    print("===============================")


if __name__ == "__main__":
    asyncio.run(run_tests())
