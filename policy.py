"""
Risk Policies for Guardian MCP defined. Every tools exposed by the server are defined with a risk tier.

SAFE      - read-only, no side effects. always executes immediately.

MODERATE  - changes state but is reversible / low-stakes. executes immediately, but every call is still written to the audit log.

HIGH      - irreversible, external-facing, or physically consequential. never executes on the first call. the server returns a
            confirmation_id instead, and the action only runs if `confirm_action` is called with that id before it expires.
 
Unknown tool names default to HIGH (fail closed, not open)  
"""

from enum import StrEnum


class RiskTier(StrEnum):
    SAFE = "safe"
    MODERATE = "moderate"
    HIGH = "high"


# HIGH risk will expire after 60 seconds if no confirmation given
CONFIRMATION_TTL_SECONDS = 60


# Tool Policies
TOOL_POLICY = {
    "list_reminders": {
        "tier": RiskTier.SAFE,
        "description": "List all reminders.",
    },
    "get_audit_log": {
        "tier": RiskTier.SAFE,
        "description": "Return the most recent audit log entries.",
    },
    "get_devices": {
        "tier": RiskTier.SAFE,
        "description": "List known smart devices and their current state.",
    },
    "add_reminder": {
        "tier": RiskTier.MODERATE,
        "description": "Create a new reminder. Reversible via delete_reminder.",
    },
    "update_reminder": {
        "tier": RiskTier.MODERATE,
        "description": "Update an existing reminder's text, due date, or done state.",
    },
    "delete_reminder": {
        "tier": RiskTier.HIGH,
        "description": "Permanently delete a reminder. Irreversible.",
    },
    "send_message": {
        "tier": RiskTier.HIGH,
        "description": "Send a message to a contact on the user's behalf. Irreversible once sent.",
    },
    "unlock_smart_lock": {
        "tier": RiskTier.HIGH,
        "description": "Unlock a physical smart lock. Has real-world physical consequences.",
    },
}



# Get the risk tier
def get_tier(tool_name: str) -> RiskTier:
    """Look up the tool's risk tier. unknown tools are automatically flagged high risk"""
    entry = TOOL_POLICY.get(tool_name)
    return entry["tier"] if entry else RiskTier.HIGH