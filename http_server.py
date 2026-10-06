#!/usr/bin/env python3
"""
HTTP server for Guardian MCP - Streamable HTTP transport per MCP spec 2025-11-25, which is what Alexa+ requires for a self-hosted MCP server.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

import server as mcp


class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[str] = None
    method: str
    params: Optional[Dict[str, Any]] = None


class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class MCPSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.initialized = False
        self.client_info: Optional[Dict] = None

    async def handle_request(self, request: MCPRequest) -> MCPResponse:
        try:
            if request.method == "initialize":
                self.client_info = request.params or {}
                self.initialized = True
                return MCPResponse(
                    id=request.id,
                    result={
                        "protocolVersion": "2025-11-25",
                        "capabilities": {
                            "tools": {"listChanged": True},
                            "resources": {"subscribe": True, "listChanged": True},
                            "prompts": {"listChanged": True},
                        },
                        "serverInfo": {"name": "guardian-mcp", "version": "0.1.0"},
                    },
                )
            elif request.method == "initialized":
                self.initialized = True
                return MCPResponse(id=request.id, result={})
            elif request.method == "tools/list":
                result = await mcp.list_tools()
                return MCPResponse(id=request.id, result={"tools": [t.model_dump() for t in result.tools]})
            elif request.method == "tools/call":
                params = request.params or {}
                result = await mcp.call_tool(params.get("name"), params.get("arguments", {}))
                return MCPResponse(
                    id=request.id,
                    result={"content": [c.model_dump() for c in result.content], "isError": result.isError},
                )
            elif request.method == "resources/list":
                result = await mcp.list_resources()
                return MCPResponse(id=request.id, result={"resources": [r.model_dump() for r in result.resources]})
            elif request.method == "resources/read":
                uri = (request.params or {}).get("uri")
                result = await mcp.read_resource(uri)
                return MCPResponse(id=request.id, result={"contents": [c.model_dump() for c in result.contents]})
            elif request.method == "prompts/list":
                return MCPResponse(id=request.id, result={"prompts": []})
            else:
                return MCPResponse(id=request.id, error={"code": -32601, "message": f"Method not found: {request.method}"})
        except Exception as e:  # noqa: BLE001 - top-level guard, mirrors JSON-RPC error shape
            return MCPResponse(id=request.id, error={"code": -32603, "message": f"Internal error: {str(e)}"})


sessions: Dict[str, MCPSession] = {}


def get_session(session_id: str) -> MCPSession:
    if session_id not in sessions:
        sessions[session_id] = MCPSession(session_id)
    return sessions[session_id]


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Guardian MCP server...")
    yield
    print("Shutting down...")


app = FastAPI(
    title="Guardian MCP",
    description="Risk-tiered, audited MCP server for Alexa+",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "name": "Guardian MCP",
        "protocol": "MCP 2025-11-25",
        "transport": "Streamable HTTP",
        "status": "running",
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    session_id = request.headers.get("Mcp-Session-Id", "default")
    session = get_session(session_id)
    body = await request.json()
    mcp_request = MCPRequest(**body)
    response = await session.handle_request(mcp_request)
    return Response(
        content=response.model_dump_json(exclude_none=True),
        media_type="application/json",
        headers={"Mcp-Session-Id": session_id},
    )


@app.get("/mcp")
async def mcp_sse(request: Request):
    session_id = request.headers.get("Mcp-Session-Id", "default")
    get_session(session_id)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'connected', 'sessionId': session_id})}\n\n"
        while True:
            await asyncio.sleep(30)
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "Mcp-Session-Id": session_id},
    )


@app.delete("/mcp/session/{session_id}")
async def delete_session(session_id: str):
    sessions.pop(session_id, None)
    return {"status": "deleted"}


# Demo-only routes
@app.get("/demo/audit-log")
async def demo_audit_log(limit: int = 50):
    return mcp.assistant.get_audit_log(limit=limit)


@app.post("/demo/confirm/{confirmation_id}")
async def demo_confirm(confirmation_id: str):
    try:
        return {"result": mcp.assistant.confirm_action(confirmation_id)}
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")


@app.post("/demo/cancel/{confirmation_id}")
async def demo_cancel(confirmation_id: str):
    try:
        return {"result": mcp.assistant.cancel_action(confirmation_id)}
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)