"""
Guardian MCP - STDIO server (for local testing; http_server.py is the Alexa+-facing transport).
"""

import asyncio
import json
import sys
from typing import Any, Dict, List

from pydantic import BaseModel

from assistant import GuardianAssistant, GuardianError
from policy import RiskTier, TOOL_POLICY, CONFIRMATION_TTL_SECONDS, get_tier

assistant = GuardianAssistant()


# MCP shapes
class ToolSchema(BaseModel):
    name: str
    description: str
    inputSchema: Dict[str, Any]


class ContentBlock(BaseModel):
    type: str = "text"
    text: str


class CallToolResult(BaseModel):
    content: List[ContentBlock]
    isError: bool = False


class ListToolsResult(BaseModel):
    tools: List[ToolSchema]


class ResourceSchema(BaseModel):
    uri: str
    name: str
    description: str
    mimeType: str = "application/json"


class ListResourcesResult(BaseModel):
    resources: List[ResourceSchema]


class ResourceContent(BaseModel):
    uri: str
    mimeType: str = "application/json"
    text: str


class ReadResourceResult(BaseModel):
    contents: List[ResourceContent]




# Tool specs
# JSON Schemas for arguments, keyed by tool name. Descriptions are pulled from policy.py so the risk tier is the single source of truth.
_TOOL_ARG_SCHEMAS = {
    "list_reminders": {"type": "object", "properties": {}},
    "get_audit_log": {
        "type": "object",
        "properties": {"limit": {"type": "integer", "default": 50}},
    },
    "get_devices": {"type": "object", "properties": {}},
    "add_reminder": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "due": {"type": "string", "description": "ISO date, optional"},
        },
        "required": ["text"],
    },
    "update_reminder": {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "string"},
            "text": {"type": "string"},
            "due": {"type": "string"},
            "done": {"type": "boolean"},
        },
        "required": ["reminder_id"],
    },
    "delete_reminder": {
        "type": "object",
        "properties": {"reminder_id": {"type": "string"}},
        "required": ["reminder_id"],
    },
    "send_message": {
        "type": "object",
        "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "body"],
    },
    "unlock_smart_lock": {
        "type": "object",
        "properties": {"device_id": {"type": "string"}},
        "required": ["device_id"],
    },
}

# Meta-tools: not domain actions, they operate on pending confirmations.
_META_TOOL_ARG_SCHEMAS = {
    "confirm_action": {
        "type": "object",
        "properties": {"confirmation_id": {"type": "string"}},
        "required": ["confirmation_id"],
    },
    "cancel_action": {
        "type": "object",
        "properties": {"confirmation_id": {"type": "string"}},
        "required": ["confirmation_id"],
    },
}


def _build_tool_list() -> List[ToolSchema]:
    tools = []
    for name, meta in TOOL_POLICY.items():
        tier_note = f" [risk tier: {meta['tier'].value}]"
        tools.append(
            ToolSchema(
                name=name,
                description=meta["description"] + tier_note,
                inputSchema=_TOOL_ARG_SCHEMAS[name],
            )
        )
    tools.append(
        ToolSchema(
            name="confirm_action",
            description=(
                f"Confirm and execute a pending HIGH-risk action within "
                f"{CONFIRMATION_TTL_SECONDS}s of it being requested."
            ),
            inputSchema=_META_TOOL_ARG_SCHEMAS["confirm_action"],
        )
    )
    tools.append(
        ToolSchema(
            name="cancel_action",
            description="Cancel a pending HIGH-risk action before it's confirmed.",
            inputSchema=_META_TOOL_ARG_SCHEMAS["cancel_action"],
        )
    )
    return tools


_TOOLS = _build_tool_list()




# MCP handlers
async def list_tools() -> ListToolsResult:
    return ListToolsResult(tools=_TOOLS)


async def call_tool(name: str, arguments: Dict[str, Any]) -> CallToolResult:
    def ok(payload: Any) -> CallToolResult:
        return CallToolResult(content=[ContentBlock(text=json.dumps(payload, default=str))])

    def err(message: str) -> CallToolResult:
        return CallToolResult(content=[ContentBlock(text=message)], isError=True)

    arguments = arguments or {}

    # Meta-tools bypass the tier gate - they ARE the gate.
    if name == "confirm_action":
        try:
            result = assistant.confirm_action(**arguments)
            return ok(result)
        except (GuardianError, TypeError) as e:
            return err(str(e))

    if name == "cancel_action":
        try:
            result = assistant.cancel_action(**arguments)
            return ok(result)
        except (GuardianError, TypeError) as e:
            return err(str(e))

    if name not in TOOL_POLICY:
        return err(f"Unknown tool '{name}'.")

    tier = get_tier(name)

    if tier == RiskTier.HIGH:
        confirmation_id = assistant.request_confirmation(name, arguments)
        return ok(
            {
                "status": "confirmation_required",
                "confirmation_id": confirmation_id,
                "expires_in_seconds": CONFIRMATION_TTL_SECONDS,
                "message": (
                    f"'{name}' is a high-risk action and was not executed. "
                    f"Call confirm_action with confirmation_id='{confirmation_id}' "
                    f"within {CONFIRMATION_TTL_SECONDS}s to run it, or cancel_action to discard it."
                ),
            }
        )

    try:
        method = getattr(assistant, name)
        result = method(**arguments)
        assistant._record(name, arguments, tier, "executed", result)
        return ok(result)
    except (GuardianError, TypeError) as e:
        assistant._record(name, arguments, tier, "denied", str(e))
        return err(str(e))


async def list_resources() -> ListResourcesResult:
    return ListResourcesResult(
        resources=[
            ResourceSchema(
                uri="assistant://audit-log",
                name="Audit log",
                description="Full history of every tool call, its risk tier, and its outcome.",
            ),
            ResourceSchema(
                uri="assistant://reminders",
                name="Reminders",
                description="All reminders.",
            ),
            ResourceSchema(
                uri="assistant://devices",
                name="Devices",
                description="Known smart devices and their state.",
            ),
        ]
    )


async def read_resource(uri: str) -> ReadResourceResult:
    if uri == "assistant://audit-log":
        data = assistant.get_audit_log(limit=200)
    elif uri == "assistant://reminders":
        data = assistant.list_reminders()
    elif uri == "assistant://devices":
        data = assistant.get_devices()
    else:
        raise ValueError(f"Unknown resource '{uri}'.")
    return ReadResourceResult(contents=[ResourceContent(uri=uri, text=json.dumps(data, default=str))])




# STDIO loop
async def _handle_line(line: str) -> Dict[str, Any]:
    req = json.loads(line)
    method = req.get("method")
    params = req.get("params") or {}
    req_id = req.get("id")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": True, "listChanged": True},
                    "prompts": {"listChanged": True},
                },
                "serverInfo": {"name": "guardian-mcp", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [t.model_dump() for t in (await list_tools()).tools]}
        elif method == "tools/call":
            r = await call_tool(params.get("name"), params.get("arguments", {}))
            result = {"content": [c.model_dump() for c in r.content], "isError": r.isError}
        elif method == "resources/list":
            result = {"resources": [r.model_dump() for r in (await list_resources()).resources]}
        elif method == "resources/read":
            r = await read_resource(params.get("uri"))
            result = {"contents": [c.model_dump() for c in r.contents]}
        else:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except Exception as e:  # noqa: BLE001 - top-level guard for the STDIO loop
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}


if __name__ == "__main__":
    # Simple synchronous read loop - fine for local testing:
    #   echo '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}' | python server.py
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        print(json.dumps(asyncio.run(_handle_line(raw_line))))
        sys.stdout.flush()