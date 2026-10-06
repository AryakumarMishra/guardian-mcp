#!/usr/bin/env python3
"""
Web simulator backend for Guardian MCP.

This is not part of the MCP spec - it is just to "simulate the Alexa+ experience in a web app" option the hackathon track explicitly allows. It runs a real tool-calling LLM (via a local Ollama server) against the exact same `mcp.call_tool()` function that server.py and
http_server.py use, so whatever you see here is provably the same guardrail logic, not a mocked-up demo.

Important design choice: `confirm_action` and `cancel_action` are deliberately NOT given to the LLM as tools. If the model could confirm its
own high-risk calls, the guardrail would be useless. Confirmation is only ever triggered by a human clicking a button in the UI, which hits /agent/confirm directly, never through the model.
"""

import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server as mcp

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = (
    "You are a helpful home voice assistant with tools for reminders, "
    "sending messages, and a smart lock.\n\n"
    "What each tier does (do not mix these up):\n"
    "- SAFE tools (list_reminders, get_devices, get_audit_log): read-only, run "
    "immediately, need NO confirmation. Never mention Confirm/Cancel for them.\n"
    "- MODERATE tools (add_reminder, update_reminder): run immediately and are "
    "done when the tool returns. Say what was created/updated. NEVER mention "
    "Confirm/Cancel buttons for them - there will be no button and asking "
    "for one confuses the user.\n"
    "- HIGH tools (delete_reminder, send_message, unlock_smart_lock): NEVER "
    "execute on the first call. The tool returns 'confirmation_required' plus "
    "a confirmation_id, and only a human clicking Confirm runs it. Only for "
    "these three tools do you ask the user to click Confirm/Cancel.\n\n"
    "ID resolution (mandatory, no exceptions):\n"
    "1. NEVER invent, guess, or ask the user for a reminder_id or device_id. "
    "The user does not know internal ids.\n"
    "2. To delete or update a reminder: FIRST call list_reminders, then call "
    "the tool with the argument named exactly 'reminder_id' (NOT 'id') set to "
    "the exact 'id' value from the results, copied character-for-character. "
    "Match by reminder text yourself. If no match, say so and stop - do not "
    "ask for the id.\n"
    "3. To unlock a lock: FIRST call get_devices, then call unlock_smart_lock "
    "with the argument named exactly 'device_id' (NOT 'id') set to 'front_door' "
    "for the Front door. "
    "'Unlock the front door' / 'open the main door' always means "
    "unlock_smart_lock with device_id='front_door'. The tool is named "
    "unlock_smart_lock (there is no unlock_front_door tool).\n"
    "4. You may chain these lookups on your own across tool rounds - do not "
    "narrate the intermediate steps or ask permission to look things up. "
    "NEVER write tool calls, JSON blobs, or anything like '{\"name\": ...}' "
    "in your chat text; the chat channel is for humans, tool calls go only "
    "through the tool channel. Announcing 'I will now call ...' instead of "
    "actually calling it leaves the job half-done.\n\n"
    "Stage-before-ask (mandatory confirmation flow):\n"
    "5. The pending entry + Confirm/Cancel buttons ARE the confirmation ask. "
    "After a lookup, you MUST immediately call the HIGH-risk tool "
    "(delete_reminder / send_message / unlock_smart_lock) in the very next "
    "tool round - do NOT stop after the lookup and ask for confirmation in "
    "text. Only the tool result 'confirmation_required' creates something to "
    "confirm.\n"
    "6. NEVER write 'please confirm in the Tool activity panel' unless the "
    "immediately preceding tool result actually said 'confirmation_required'. "
    "If you have not called the HIGH-risk tool yet, you have nothing to "
    "confirm - call the tool first.\n"
    "7. Correct chains (follow these exactly):\n"
    "   - 'delete that reminder' -> list_reminders -> "
    "delete_reminder(reminder_id='<exact id from results>') -> then tell the "
    "user to click Confirm/Cancel.\n"
    "   - 'unlock the front door' -> get_devices -> "
    "unlock_smart_lock(device_id='front_door') -> then tell the user to "
    "click Confirm/Cancel.\n\n"
    "Errors and confirmation:\n"
    "8. If a tool call returns an error, read the error message and retry "
    "with corrected arguments (usually by calling list_reminders/get_devices "
    "first and copying the exact 'id' character-for-character - placeholders "
    "like 'id from the reminder' are never valid). Do not give up and invent "
    "a workaround.\n"
    "9. delete_reminder, send_message, and unlock_smart_lock are HIGH risk: "
    "the tool result will say 'confirmation_required' and the action is NOT "
    "done yet. You MUST reply with: what you want to do (naming the reminder "
    "text or device), that it is HIGH risk / irreversible, and that the user "
    "must click Confirm or Cancel in the Pending approvals / Tool activity "
    "panel. NEVER say the action is done, sent, deleted, or unlocked before "
    "the human confirms.\n"
    "10. There is no separate app. Everything happens through these tools "
    "and this chat. Never mention a 'Guardian Assistant app' or any other "
    "product that doesn't exist."
)

app = FastAPI(title="Guardian MCP - web simulator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, Any]]] = None


def _agent_tools() -> List[Dict[str, Any]]:
    """Tool schemas for the LLM - everything except the confirm/cancel
    meta-tools, which only the human-facing UI is allowed to call."""
    tools = []
    for t in mcp._TOOLS:
        if t.name in ("confirm_action", "cancel_action"):
            continue
        tools.append({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}})
    return tools


def _ollama_chat(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    resp = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "messages": messages, "tools": _agent_tools(), "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"]


import re


def _strip_narrated_tool_json(reply: str) -> str:
    """Remove tool-call JSON the model leaked into chat text.

    Small models often write '{"name": "delete_reminder", ...}' as prose
    instead of (or as well as) a real tool call. That text never executes
    anything and confuses users, so strip it; the real trace lives in the
    Tool activity panel. Handles nested braces via balanced-brace scan.
    """
    if not reply or '"name"' not in reply and "'name'" not in reply:
        return reply
    out = []
    i, n = 0, len(reply)
    while i < n:
        start = reply.find('{"name"', i)
        alt = reply.find("{'name'", i)
        if alt != -1 and (start == -1 or alt < start):
            start = alt
        if start == -1:
            out.append(reply[i:])
            break
        # Only treat as narrated tool JSON if it precedes a plausible tool key.
        out.append(reply[i:start])
        depth = 0
        j = start
        in_str: Optional[str] = None
        while j < n:
            ch = reply[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        i = j  # skip the whole JSON blob
    cleaned = "".join(out)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _high_intent(user_message: str) -> Optional[str]:
    """Classify an obvious HIGH-risk intent, else None (best-effort nudge only)."""
    t = (user_message or "").lower()
    if "delet" in t and "remind" in t:
        return "delete_reminder"
    if ("remov" in t or "forget" in t) and "remind" in t:
        return "delete_reminder"
    if "unlock" in t and ("door" in t or "lock" in t or "front" in t):
        return "unlock_smart_lock"
    if "open" in t and ("front door" in t or "main door" in t or "smart lock" in t):
        return "unlock_smart_lock"
    if "send" in t and ("message" in t or "text" in t or "sms" in t):
        return "send_message"
    return None


def _needs_continuation(user_message: str, tool_trace: List[Dict[str, Any]],
                        pending: Optional[Dict[str, Any]]) -> Optional[str]:
    """Detect lookup-only stall: intent is HIGH-risk but no HIGH tool ran yet."""
    if pending is not None or not tool_trace:
        return None
    intent = _high_intent(user_message)
    if intent is None:
        return None
    names = [c.get("name", "") for c in tool_trace]
    if intent in names:
        return None  # HIGH tool ran (staged or denied with retry instructions)
    lookups = {"list_reminders", "get_devices", "get_audit_log"}
    if all(n in lookups for n in names):
        return intent
    return None


CONTINUATION_NUDGE = (
    "[Automated guardrail nudge, not from the user] You stopped after the "
    "lookup, but the HIGH-risk action is NOT staged yet - no confirmation "
    "exists and nothing will happen. The lookup result is in the tool "
    "response above. NOW call the HIGH-risk tool immediately through the "
    "tool channel (never in chat text): for a delete use "
    "delete_reminder with argument reminder_id set to the exact id value; "
    "for unlock use unlock_smart_lock with device_id='front_door'. "
    "Do not narrate or ask - just call it."
)


def _mentions_confirmation(text: str) -> bool:
    """Detect the model promising a Confirm button that may not exist."""
    if not text:
        return False
    t = text.lower()
    return ("confirm" in t and ("panel" in t or "button" in t or "tool activity" in t)) or (
        "requires your confirmation" in t
    )


FALSE_CONFIRM_CORRECTION = (
    " (Note: no HIGH-risk action was actually staged for that request, so there "
    "is nothing to confirm yet - the Tool activity panel correctly shows no "
    "Confirm button. I did not execute anything. To proceed I will look up "
    "the exact id and stage the action so a Confirm button appears.)"
)


async def _run_agent(user_message: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": user_message}]
    tool_trace: List[Dict[str, Any]] = []
    pending_confirmation: Optional[Dict[str, Any]] = None
    had_denied_high_risk = False

    for _ in range(MAX_TOOL_ROUNDS):
        assistant_msg = _ollama_chat(messages)
        messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            reply = _strip_narrated_tool_json(assistant_msg.get("content", ""))
            # Server-side enforcement: if the last action needs confirmation,
            # never let a weak model claim it is already done.
            if pending_confirmation:
                reply = _confirmation_prompt(pending_confirmation, reply)
            elif _needs_continuation(user_message, tool_trace, pending_confirmation) is not None:
                # Lookup-only stall: the model announced instead of staging.
                # Give it one forced extra round with the lookup result in
                # context rather than ending the turn half-done (this is what
                # previously forced the user to re-prompt "did you do it?").
                messages.append({"role": "user", "content": CONTINUATION_NUDGE})
                nudged_rounds = 0
                while nudged_rounds < 2:
                    nudged_rounds += 1
                    assistant_msg = _ollama_chat(messages)
                    messages.append(assistant_msg)
                    tool_calls = assistant_msg.get("tool_calls") or []
                    if not tool_calls:
                        break
                    for call in tool_calls:
                        fn = call.get("function", {}) if isinstance(call, dict) else {}
                        name = fn.get("name", "")
                        raw_args = fn.get("arguments", {})
                        try:
                            arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                        except (json.JSONDecodeError, TypeError):
                            continue
                        result = await mcp.call_tool(name, arguments)
                        result_text = result.content[0].text
                        tool_trace.append({"name": name, "arguments": arguments, "result": result_text, "isError": result.isError})
                        if result.isError and name in ("delete_reminder", "send_message", "unlock_smart_lock"):
                            had_denied_high_risk = True
                        tool_msg: Dict[str, Any] = {"role": "tool", "content": result_text}
                        call_id = call.get("id") if isinstance(call, dict) else None
                        if call_id:
                            tool_msg["tool_call_id"] = call_id
                        if name:
                            tool_msg["tool_name"] = name
                        messages.append(tool_msg)
                        try:
                            parsed = json.loads(result_text)
                        except (json.JSONDecodeError, TypeError):
                            parsed = None
                        if isinstance(parsed, dict) and parsed.get("status") == "confirmation_required":
                            pending_confirmation = {
                                "confirmation_id": parsed.get("confirmation_id"),
                                "tool": name,
                                "arguments": arguments,
                                "expires_in_seconds": parsed.get("expires_in_seconds", 60),
                            }
                    if pending_confirmation or had_denied_high_risk:
                        break
                reply = _strip_narrated_tool_json(assistant_msg.get("content", ""))
                if pending_confirmation:
                    reply = _confirmation_prompt(pending_confirmation, reply)
                elif _mentions_confirmation(reply):
                    reply = reply + FALSE_CONFIRM_CORRECTION
            elif _mentions_confirmation(reply):
                # Opposite failure: model promised a Confirm button without
                # ever staging the HIGH-risk tool (only a lookup ran, or the
                # stage was denied on a placeholder id). Correct it inline so
                # the user is not sent hunting for a button that cannot exist.
                reply = reply + FALSE_CONFIRM_CORRECTION
            return {"reply": reply, "tool_trace": tool_trace, "history": messages[1:],
                    "pending_confirmation": pending_confirmation}

        for call in tool_calls:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})

            result = await mcp.call_tool(name, arguments)
            result_text = result.content[0].text
            tool_trace.append({"name": name, "arguments": arguments, "result": result_text, "isError": result.isError})
            if result.isError and name in ("delete_reminder", "send_message", "unlock_smart_lock"):
                had_denied_high_risk = True

            tool_msg: Dict[str, Any] = {"role": "tool", "content": result_text}
            # Link tool responses to their calls (Ollama/OpenAI-style). Falls
            # back gracefully for models that omit ids.
            call_id = call.get("id") if isinstance(call, dict) else None
            if call_id:
                tool_msg["tool_call_id"] = call_id
            if name:
                tool_msg["tool_name"] = name
            messages.append(tool_msg)

            try:
                parsed = json.loads(result_text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("status") == "confirmation_required":
                pending_confirmation = {
                    "confirmation_id": parsed.get("confirmation_id"),
                    "tool": name,
                    "arguments": arguments,
                    "expires_in_seconds": parsed.get("expires_in_seconds", 60),
                }

    reply = "I'm having trouble finishing that - try rephrasing, or check the tool activity panel for what happened."
    if pending_confirmation:
        reply = _confirmation_prompt(pending_confirmation, "")
    elif had_denied_high_risk:
        reply = (
            "That HIGH-risk action could not be staged - the id sent to the tool "
            "was invalid (see the error badge in Tool activity). Nothing was "
            "executed and there is nothing to confirm. Say 'retry' and I will "
            "re-read the exact id from list_reminders/get_devices and stage it again."
        )
    return {
        "reply": reply,
        "tool_trace": tool_trace,
        "history": messages[1:],
        "pending_confirmation": pending_confirmation,
    }


def _confirmation_prompt(pending: Dict[str, Any], model_reply: str) -> str:
    """Deterministic confirmation ask. Used when the model forgets Rule 6."""
    tool = pending.get("tool", "action")
    args = pending.get("arguments", {})
    target = args.get("reminder_id") or args.get("device_id") or args.get("to") or json.dumps(args)
    base = (
        f"I want to run HIGH-risk '{tool}' ({target}). "
        "This is irreversible / physically consequential and is NOT done yet. "
        "Please click Confirm or Cancel in the Tool activity panel."
    )
    if model_reply and "confirm" in model_reply.lower():
        return model_reply
    return base




@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.post("/agent/chat")
async def agent_chat(req: ChatRequest):
    try:
        result = await _run_agent(req.message, req.history or [])
        return JSONResponse(result)
    except requests.exceptions.ConnectionError:
        return JSONResponse(
            {"error": f"Can't reach Ollama at {OLLAMA_URL}. Is `ollama serve` running?"},
            status_code=503,
        )


@app.get("/agent/audit-log")
async def agent_audit_log(limit: int = 50):
    return mcp.assistant.get_audit_log(limit=limit)


@app.get("/agent/pending")
async def agent_pending():
    """Staged HIGH-risk actions awaiting human Confirm/Cancel. Polled by the UI."""
    return mcp.assistant.list_pending()


@app.post("/agent/confirm/{confirmation_id}")
async def agent_confirm(confirmation_id: str):
    try:
        result = mcp.assistant.confirm_action(confirmation_id)
        return {
            "result": result,
            "followup": f"Confirmed and executed: {json.dumps(result, default=str)}",
        }
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/agent/cancel/{confirmation_id}")
async def agent_cancel(confirmation_id: str):
    try:
        result = mcp.assistant.cancel_action(confirmation_id)
        return {
            "result": result,
            "followup": "Cancelled. Nothing was executed.",
        }
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/agent/reset")
async def agent_reset():
    # Clean the canonical store plus the legacy web_demo/data dir left over
    # from when GuardianAssistant used a CWD-relative path.
    legacy_dir = os.path.join(os.path.dirname(__file__), "data")
    for d in {mcp.assistant.data_dir, legacy_dir}:
        if os.path.isdir(d):
            shutil.rmtree(d)
    mcp.assistant = mcp.GuardianAssistant()
    return {"status": "reset"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8001)))