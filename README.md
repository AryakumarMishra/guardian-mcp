# Guardian MCP
 
A self-hosted MCP server for Alexa+ (Build, Ship, Shape hackathon, Alexa+ track) that adds a permission and audit layer most agentic tool servers don't have.

This project is demonstrated via a chat UI that simulates the Alexa+ experience: a real tool-calling LLM (run locally via Ollama, so no API key or cost) talks to the exact same `call_tool()` function the MCP server uses, so the guardrail that is seen here is the real one, not a mock.
 
## The idea
 
Most MCP servers expose tools flatly. An LLM can call `delete_x` with the same friction as `list_x`. Guardian MCP tags every tool with a **risk tier** and enforces different behavior per tier:
 
| Tier     | Examples                              | Behavior                                   |
|----------|----------------------------------------|---------------------------------------------|
| SAFE     | `list_reminders`, `get_audit_log`      | Runs immediately                           |
| MODERATE | `add_reminder`, `update_reminder`      | Runs immediately, logged                   |
| HIGH     | `delete_reminder`, `send_message`, `unlock_smart_lock` | **Not** run on first call - returns a `confirmation_id` instead. Only `confirm_action` (within 60s) actually executes it. |
 
Every tool call - executed, pending, confirmed, expired, cancelled, or denied - is written to an append-only audit log, exposed as the
`assistant://audit-log` MCP resource.

## Rough Architecture Diagram

![Guardian-MCP-Architectue](image/guardian_mcp_architecture.png)
 
## Project structure
 
```
guardian-mcp/
├── policy.py         # risk tiers + which tools are in which tier
├── storage.py         # thread-safe JSON file storage
├── assistant.py        # domain logic + audit log + confirmation gate
├── server.py            # STDIO MCP server (local testing) + tool/resource schemas
├── http_server.py        # Streamable HTTP transport (what Alexa+ connects to)
├── requirements.txt
└── data/                 # created at runtime, gitignored
```

## Running it (via Terminal)
 
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
 
# STDIO (local testing)
echo '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}' | python server.py
 
# HTTP (Alexa+-facing)
python3 http_server.py
curl http://localhost:8000/health
```
 
## Trying the guardrail flow with curl
 
```bash
# A SAFE tool - runs immediately
curl -s -X POST localhost:8000/mcp -H 'Content-Type: application/json' \
  -H 'Mcp-Session-Id: demo' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"add_reminder","arguments":{"text":"call the landlord"}}}'
 
# A HIGH-risk tool - note this does NOT delete anything yet
curl -s -X POST localhost:8000/mcp -H 'Content-Type: application/json' \
  -H 'Mcp-Session-Id: demo' \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/call","params":{"name":"delete_reminder","arguments":{"reminder_id":"<id-from-step-1>"}}}'
# -> returns a confirmation_id and a 60s expiry
 
# Confirm it (within 60s) to actually execute
curl -s -X POST localhost:8000/demo/confirm/<confirmation-id>
 
# Inspect the full audit trail
curl -s localhost:8000/demo/audit-log | python3 -m json.tool
```

## Running it (via Web Demo)

### One-time setup
 
```bash
# Install Ollama: https://ollama.com/download
ollama pull llama3.2
 
# Install Python deps (from the guardian-mcp/ folder)
cd ..
pip install -r requirements.txt
cd web_demo
```
 
### Running
 
```bash
# Terminal 1
ollama serve
 
# Terminal 2
python3 agent_server.py
# open http://localhost:8001
```