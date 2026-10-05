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
MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = (
    "You are a helpful home voice assistant with tools for reminders, "
    "sending messages, and a smart lock.\n\n"
    "Hard rules:\n"
    "1. Never invent an id. To delete or update a reminder, first call "
    "list_reminders and use the exact 'id' field from its results - never "
    "use a placeholder or made-up id. To act on a device, first call "
    "get_devices and use the exact 'id' field from its results.\n"
    "2. If a tool call returns an error, read the error message and retry "
    "with corrected arguments. Do not give up and invent a workaround.\n"
    "3. There is no separate app. Everything happens through these tools "
    "and this chat. Never mention a 'Guardian Assistant app' or any other "
    "product that doesn't exist - if you're unsure what to do, ask the "
    "user a direct question instead.\n"
    "4. If a tool result says a confirmation is required, tell the user "
    "plainly what you want to do and that it needs their approval via the "
    "Confirm/Cancel buttons in this chat - do not say the action is done."
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


async def _run_agent(user_message: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": user_message}]
    tool_trace: List[Dict[str, Any]] = []

    for _ in range(MAX_TOOL_ROUNDS):
        assistant_msg = _ollama_chat(messages)
        messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls") or []
        if not tool_calls:
            return {"reply": assistant_msg.get("content", ""), "tool_trace": tool_trace, "history": messages[1:]}

        for call in tool_calls:
            name = call["function"]["name"]
            raw_args = call["function"].get("arguments", {})
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})

            result = await mcp.call_tool(name, arguments)
            result_text = result.content[0].text
            tool_trace.append({"name": name, "arguments": arguments, "result": result_text, "isError": result.isError})

            messages.append({"role": "tool", "content": result_text})

    return {
        "reply": "I'm having trouble finishing that - try rephrasing, or check the tool activity panel for what happened.",
        "tool_trace": tool_trace,
        "history": messages[1:],
    }




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


@app.post("/agent/confirm/{confirmation_id}")
async def agent_confirm(confirmation_id: str):
    try:
        return {"result": mcp.assistant.confirm_action(confirmation_id)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/agent/cancel/{confirmation_id}")
async def agent_cancel(confirmation_id: str):
    try:
        return {"result": mcp.assistant.cancel_action(confirmation_id)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/agent/reset")
async def agent_reset():
    if os.path.isdir(mcp.assistant.data_dir):
        shutil.rmtree(mcp.assistant.data_dir)
    mcp.assistant = mcp.GuardianAssistant()
    return {"status": "reset"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8001)))