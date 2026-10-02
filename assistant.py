"""
GuardianAssistant: the domain logic (reminders, messages, smart devices) plus the two pieces that make this different from a plain CRUD assistant:
 
  1. An append-only audit log: every tool call, its risk tier, and its outcome (executed / pending / confirmed / expired / cancelled / denied) gets written here. Nothing is silently dropped.
  2. A confirmation gate for HIGH-risk actions: `request_confirmation` parks the action instead of running it, and only `confirm_action` actually executes it, within a TTL.
"""

import uuid
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from storage import JSONStore

from policy import RiskTier, CONFIRMATION_TTL_SECONDS


# User facing error class
class GuardianError(Exception):
    """Raised for expected, user-facing failures (bad id, expired
    confirmation, etc). server.py catches this and reports it as a normal
    tool error rather than an internal server error."""
 

# Guardian Assistant class
class GuardianAssistant:
    def __init__(self, data_dir: str = "data"):
        # if data_dir in None:
        #     data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

        # self.data_dir = data_dir
        self.reminders = JSONStore(f"{data_dir}/reminders.json", [])
        self.messages = JSONStore(f"{data_dir}/messages.json", [])
        self.devices = JSONStore(
            f"{data_dir}/devices.json",
            [{"id": "front_door", "name": "Front door", "locked": True}],
        )
        self.audit_log = JSONStore(f"{data_dir}/audit_log.json", [])
        self._pending: Dict[str, Dict[str, Any]] = {}


    # Audit
    def _record(
        self,
        tool: str,
        arguments: Dict[str, Any],
        tier: RiskTier,
        status: str,
        result: Any = None,
    ) -> Dict[str, Any]:

        risk_tier_value = tier.value if isinstance(tier, RiskTier) else str(tier)
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "tool": tool,
            "arguments": arguments,
            "risk_tier": risk_tier_value,
            "status": status,  # executed | pending_confirmation | confirmed_and_executed | expired | cancelled | denied
            "result": result,
        }
        log = self.audit_log.read()
        log.append(entry)
        self.audit_log.write(log)
        return entry

 
    def get_audit_log(self, limit: int = 50) -> list:
        return self.audit_log.read()[-limit:]



    # Reminders
    def list_reminders(self) -> list:
        return self.reminders.read()
 
    def add_reminder(self, text: str, due: Optional[str] = None) -> dict:
        reminders = self.reminders.read()
        item = {"id": str(uuid.uuid4()), "text": text, "due": due, "done": False}
        reminders.append(item)
        self.reminders.write(reminders)
        return item
 
    def update_reminder(
        self,
        reminder_id: str,
        text: Optional[str] = None,
        due: Optional[str] = None,
        done: Optional[bool] = None,
    ) -> dict:
        reminders = self.reminders.read()
        for r in reminders:
            if r["id"] == reminder_id:
                if text is not None:
                    r["text"] = text
                if due is not None:
                    r["due"] = due
                if done is not None:
                    r["done"] = done
                self.reminders.write(reminders)
                return r
        raise GuardianError(f"No reminder with id '{reminder_id}'.")
 
    def delete_reminder(self, reminder_id: str) -> dict:
        """HIGH risk - only ever called by confirm_action, never directly."""
        reminders = self.reminders.read()
        kept = [r for r in reminders if r["id"] != reminder_id]
        if len(kept) == len(reminders):
            raise GuardianError(f"No reminder with id '{reminder_id}'.")
        self.reminders.write(kept)
        return {"deleted": reminder_id}



    # Messages and Lock
    def send_message(self, to: str, body: str) -> dict:
        """HIGH risk. This is a demo and just logs the message. Can be connected with Twilio, SES, etc."""
        messages = self.messages.read()
        msg = {
            "id": str(uuid.uuid4()),
            "to": to,
            "body": body,
            "sent_at": datetime.utcnow().isoformat() + "Z",
        }
        messages.append(msg)
        self.messages.write(messages)
        return msg
 
    def get_devices(self) -> list:
        return self.devices.read()
 
    def unlock_smart_lock(self, device_id: str) -> dict:
        """HIGH risk - has a real-world physical consequence."""
        devices = self.devices.read()
        for d in devices:
            if d["id"] == device_id:
                d["locked"] = False
                self.devices.write(devices)
                return d
        raise GuardianError(f"No device with id '{device_id}'.")



    # Confirmation Gate
    def request_confirmation(self, tool: str, arguments: Dict[str, Any]) -> str:
        confirmation_id = str(uuid.uuid4())
        self._pending[confirmation_id] = {
            "tool": tool,
            "arguments": arguments,
            "expires_at": datetime.now(datetime.timezone.utc) + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
        }
        self._record(tool, arguments, RiskTier.HIGH, "pending_confirmation", {"confirmation_id": confirmation_id})
        return confirmation_id
 
    def confirm_action(self, confirmation_id: str) -> Any:
        pending = self._pending.get(confirmation_id)
        if pending is None:
            raise GuardianError("Unknown or already-used confirmation id.")
        if datetime.now(datetime.timezone.utc) > pending["expires_at"]:
            del self._pending[confirmation_id]
            self._record(pending["tool"], pending["arguments"], RiskTier.HIGH, "expired")
            raise GuardianError("Confirmation window expired - re-issue the original tool call.")
 
        del self._pending[confirmation_id]
        tool, arguments = pending["tool"], pending["arguments"]
        method = getattr(self, tool)
        result = method(**arguments)
        self._record(tool, arguments, RiskTier.HIGH, "confirmed_and_executed", result)
        return result
 
    def cancel_action(self, confirmation_id: str) -> dict:
        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            raise GuardianError("Unknown or already-used confirmation id.")
        self._record(pending["tool"], pending["arguments"], RiskTier.HIGH, "cancelled")
        return {"cancelled": confirmation_id}
