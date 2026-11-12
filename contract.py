"""contract.py - 共享契约与严格参数校验

定义 LivingMemory 桥接的统一协议契约 (coopanion.memory-bridge.v1) 与
工具模式版本 (coopanion.livingmemory.tools.v1)。
提供严格的字段级参数校验，拒绝未知字段、缺失必填项与类型不匹配。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

CONTRACT_FILE = Path(__file__).resolve().parent / "contract.json"

with open(CONTRACT_FILE, "r", encoding="utf-8") as f:
    _CONTRACT_DATA = json.load(f)

SCHEMA_VERSION: str = _CONTRACT_DATA.get("schema_version", "coopanion.memory-bridge.v1")
TOOLS_SCHEMA_VERSION: str = _CONTRACT_DATA.get("tools_schema_version", "coopanion.livingmemory.tools.v1")
ALLOWED_FIELDS: list[str] = _CONTRACT_DATA.get("allowed_fields", [])
TOOLS_DEF: dict[str, dict[str, Any]] = _CONTRACT_DATA.get("tools", {})


def get_contract_data() -> dict[str, Any]:
    """获取完整的契约规范数据"""
    return dict(_CONTRACT_DATA)


def validate_tool_params(
    tool_name: str,
    params: dict[str, Any],
) -> tuple[bool, Optional[dict[str, Any]]]:
    """严格校验工具参数。

    规则：
    1. additionalProperties: false - 拒绝任何未在 schema 中声明的未知字段。
    2. required - 拒绝缺失必填字段，且字符串必填项不可为空白。
    3. 类型检查 - 严格匹配 schema 声明的类型（string, integer, number, boolean, array）。
    4. 数组元素类型检查 - 检查 array[items.type]。
    5. 枚举检查 - 若声明 enum，值必须在枚举集合中。

    Returns:
        (True, None) 如果通过校验；
        (False, error_dict) 如果校验失败，包含 code, message, field, tool, reason。
    """
    tool = TOOLS_DEF.get(tool_name)
    if not tool:
        return False, {
            "ok": False,
            "code": "unknown_tool",
            "tool": tool_name,
            "message": f"未知的工具名称: {tool_name}",
            "reason": f"支持的工具列表: {list(TOOLS_DEF.keys())}",
        }

    schema = tool.get("parameters", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])

    # 1. 拒绝未知字段 (additionalProperties: false)
    for key in params:
        if key not in props:
            return False, {
                "ok": False,
                "code": "unknown_field",
                "field": key,
                "tool": tool_name,
                "message": f"未知参数 '{key}'",
                "reason": f"工具 '{tool_name}' 允许的参数字段: {list(props.keys())}",
            }

    # 2. 检查必填项 (required)
    for req_field in required:
        if req_field not in params or params[req_field] is None:
            return False, {
                "ok": False,
                "code": "missing_field",
                "field": req_field,
                "tool": tool_name,
                "message": f"缺少必填参数 '{req_field}'",
                "reason": f"参数 '{req_field}' 为必填项",
            }
        val = params[req_field]
        if isinstance(val, str) and not val.strip():
            return False, {
                "ok": False,
                "code": "missing_field",
                "field": req_field,
                "tool": tool_name,
                "message": f"必填参数 '{req_field}' 不能为空字符串",
                "reason": "字符串内容不可为空白",
            }

    # 3. 字段类型与枚举检查
    for field_name, val in params.items():
        if val is None:
            continue
        field_schema = props.get(field_name, {})
        expected_type = field_schema.get("type")

        if expected_type == "string":
            if not isinstance(val, str):
                return False, {
                    "ok": False,
                    "code": "invalid_type",
                    "field": field_name,
                    "tool": tool_name,
                    "message": f"参数 '{field_name}' 类型错误: 期望 string，实际为 {type(val).__name__}",
                    "reason": "期望 string 类型",
                }
            enums = field_schema.get("enum")
            if enums and val not in enums:
                return False, {
                    "ok": False,
                    "code": "invalid_value",
                    "field": field_name,
                    "tool": tool_name,
                    "message": f"参数 '{field_name}' 取值无效: '{val}' 不在允许的列表中",
                    "reason": f"允许的取值: {enums}",
                }

        elif expected_type == "integer":
            if not isinstance(val, int) or isinstance(val, bool):
                return False, {
                    "ok": False,
                    "code": "invalid_type",
                    "field": field_name,
                    "tool": tool_name,
                    "message": f"参数 '{field_name}' 类型错误: 期望 integer，实际为 {type(val).__name__}",
                    "reason": "期望 integer 类型",
                }

        elif expected_type == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return False, {
                    "ok": False,
                    "code": "invalid_type",
                    "field": field_name,
                    "tool": tool_name,
                    "message": f"参数 '{field_name}' 类型错误: 期望 number，实际为 {type(val).__name__}",
                    "reason": "期望 number 类型",
                }

        elif expected_type == "boolean":
            if not isinstance(val, bool):
                return False, {
                    "ok": False,
                    "code": "invalid_type",
                    "field": field_name,
                    "tool": tool_name,
                    "message": f"参数 '{field_name}' 类型错误: 期望 boolean，实际为 {type(val).__name__}",
                    "reason": "期望 boolean 类型",
                }

        elif expected_type == "array":
            if not isinstance(val, list):
                return False, {
                    "ok": False,
                    "code": "invalid_type",
                    "field": field_name,
                    "tool": tool_name,
                    "message": f"参数 '{field_name}' 类型错误: 期望 array，实际为 {type(val).__name__}",
                    "reason": "期望 array 列表类型",
                }
            item_schema = field_schema.get("items", {})
            item_type = item_schema.get("type")
            if item_type == "string":
                for item in val:
                    if not isinstance(item, str):
                        return False, {
                            "ok": False,
                            "code": "invalid_type",
                            "field": field_name,
                            "tool": tool_name,
                            "message": f"数组 '{field_name}' 中的元素必须全部为 string",
                            "reason": "列表元素期望 string 类型",
                        }
            elif item_type == "integer":
                for item in val:
                    if not isinstance(item, int) or isinstance(item, bool):
                        return False, {
                            "ok": False,
                            "code": "invalid_type",
                            "field": field_name,
                            "tool": tool_name,
                            "message": f"数组 '{field_name}' 中的元素必须全部为 integer",
                            "reason": "列表元素期望 integer 类型",
                        }

    return True, None
