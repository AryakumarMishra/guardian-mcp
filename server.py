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
            "reminder_id": {"type": "string", "description": "Exact 'id' from list_reminders. Call list_reminders first."},
            "text": {"type": "string"},
            "due": {"type": "string"},
            "done": {"type": "boolean"},
        },
        "required": ["reminder_id"],
    },
    "delete_reminder": {
        "type": "object",
        "properties": {"reminder_id": {"type": "string", "description": "Exact 'id' from list_reminders. Call list_reminders first, never invent it."}},
        "required": ["reminder_id"],
    },
    "send_message": {
        "type": "object",
        "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "body"],
    },
    "unlock_smart_lock": {
        "type": "object",
        "properties": {
            "device_id": {
                "type": "string",
                "description": "Exact 'id' from get_devices, e.g. 'front_door'. Call get_devices first. Aliases 'front door', 'front-door', 'main door' also work.",
            }
        },
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


def _looks_like_placeholder(value: str) -> bool:
    """Strict fail-closed check: placeholders are never valid ids.

    Catches the small-model failure mode of sending literal text like
    'id from the reminder to be deleted' instead of copying the exact
    'id' from list_reminders/get_devices results.
    """
    if not isinstance(value, str):
        return True
    v = value.strip()
    if not v:
        return True
    lowered = v.lower()
    markers = ("id from", "reminder to", "from the reminder", "placeholder",
               "example", "<", ">", "your-", "_id_here", "insert ", "todo")
    if any(m in lowered for m in markers):
        return True
    if " " in v or len(v) > 80:
        return True
    return False


# Lenient aliases for the small-model failure mode of using the result field
# name ("id") instead of the tool argument name ("reminder_id"/"device_id").
# Canonical names always win; aliases only fill in when canonical is absent.
_ARG_ALIASES = {
    "delete_reminder": {"reminder_id": ("id", "reminderId", "reminder-id")},
    "update_reminder": {"reminder_id": ("id", "reminderId", "reminder-id")},
    "unlock_smart_lock": {"device_id": ("id", "deviceId", "device-id", "device", "lock_id")},
}


def _apply_arg_aliases(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    arguments = dict(arguments or {})
    for canonical, aliases in _ARG_ALIASES.get(name, {}).items():
        if arguments.get(canonical) in (None, ""):
            for a in aliases:
                if arguments.get(a) not in (None, ""):
                    arguments[canonical] = arguments[a]
                    break
        # Drop alias keys so method(**arguments) never sees duplicates.
        for a in aliases:
            arguments.pop(a, None)
    return arguments


def _validate_high_risk_args(name: str, arguments: Dict[str, Any]) -> Any:
    """Return an error message if HIGH-risk args are clearly unexecutable, else None."""
    from assistant import _normalize_device_id

    if name == "delete_reminder":
        rid = (arguments or {}).get("reminder_id")
        if not rid or not isinstance(rid, str):
            return "Missing 'reminder_id'. Call list_reminders first and use an exact 'id' from its results - never ask the user for an id."
        if _looks_like_placeholder(rid):
            return (
                f"That 'reminder_id' ({rid!r}) looks like placeholder text, not a real id. "
                "Copy the exact 'id' string from the list_reminders result above character-for-character "
                "and retry delete_reminder with it - never paraphrase or describe the id."
            )
        if not assistant.has_reminder(rid):
            return f"No reminder with id '{rid}'. Call list_reminders first and use an exact 'id' from its results."
    elif name == "send_message":
        to = (arguments or {}).get("to")
        body = (arguments or {}).get("body")
        if not to or not body:
            return "Missing 'to' and/or 'body'. Both are required to send a message."
    elif name == "unlock_smart_lock":
        did = (arguments or {}).get("device_id")
        if not did or not isinstance(did, str):
            return "Missing 'device_id'. Call get_devices first and use an exact 'id' from its results - never ask the user for an id."
        normalized = _normalize_device_id(did)
        if not assistant.has_device(normalized):
            return (
                f"No device with id '{did}'. Valid ids: {assistant.valid_device_ids()}. "
                "Call get_devices first and use an exact 'id' from its results."
            )
    return None




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

    # Lenient alias mapping first (model often sends {"id": ...} instead of
    # {"reminder_id": ...}); canonical names always win.
    arguments = _apply_arg_aliases(name, arguments)

    if tier == RiskTier.HIGH:
        # Fail fast on bad ids BEFORE creating a pending confirmation, so the
        # LLM learns to call list_reminders/get_devices instead of asking the
        # user for an id or requesting confirmation on something unexecutable.
        validation_error = _validate_high_risk_args(name, arguments)
        if validation_error is not None:
            assistant._record(name, arguments, tier, "denied", validation_error)
            return err(validation_error)
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