"""
GuardianAssistant: the domain logic (reminders, messages, smart devices) plus the two pieces that make this different from a plain CRUD assistant:
 
  1. An append-only audit log: every tool call, its risk tier, and its outcome (executed / pending / confirmed / expired / cancelled / denied) gets written here. Nothing is silently dropped.
  2. A confirmation gate for HIGH-risk actions: `request_confirmation` parks the action instead of running it, and only `confirm_action` actually executes it, within a TTL.
"""

import uuid
import os
import datetime
from datetime import timedelta
from typing import Any, Dict, Optional
from storage import JSONStore

from policy import RiskTier, CONFIRMATION_TTL_SECONDS


# Canonical device ids and human-friendly aliases the LLM / user may utter.
# "front door", "front-door", "main door" all mean the "front_door" device.
DEVICE_ALIASES = {
    "front_door": "front_door",
    "front door": "front_door",
    "front-door": "front_door",
    "frontdoor": "front_door",
    "main door": "front_door",
    "main-door": "front_door",
    "front gate": "front_door",
}


def _normalize_device_id(raw: Any) -> str:
    """Map aliases / case / whitespace variants to the canonical device id."""
    if not isinstance(raw, str):
        return raw
    key = raw.strip().lower()
    return DEVICE_ALIASES.get(key, raw.strip())


# User facing error class
class GuardianError(Exception):
    """Raised for expected, user-facing failures (bad id, expired
    confirmation, etc). server.py catches this and reports it as a normal
    tool error rather than an internal server error."""
 

# Guardian Assistant class
class GuardianAssistant:
    def __init__(self, data_dir: Optional[str] = None):
        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

        self.data_dir = data_dir
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

        #Fail closed: never let a non-tier value (e.g. a regressed get_tier returning a list/dict) poison the audit log as a raw repr string.
        if isinstance(tier, RiskTier):
            risk_tier_value = tier.value
        elif isinstance(tier, str) and tier in ("safe", "moderate", "high"):
            risk_tier_value = tier
        else:
            risk_tier_value = RiskTier.HIGH.value
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
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
        raise GuardianError(f"No reminder with id '{reminder_id}'. Call list_reminders first and use an exact 'id' from its results.")
 
    def delete_reminder(self, reminder_id: str) -> dict:
        """HIGH risk - only ever called by confirm_action, never directly."""
        reminders = self.reminders.read()
        kept = [r for r in reminders if r["id"] != reminder_id]
        if len(kept) == len(reminders):
            raise GuardianError(f"No reminder with id '{reminder_id}'. Call list_reminders first and use an exact 'id' from its results.")
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
            "sent_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        messages.append(msg)
        self.messages.write(messages)
        return msg
 
    def get_devices(self) -> list:
        return self.devices.read()
 
    def unlock_smart_lock(self, device_id: str) -> dict:
        """HIGH risk - has a real-world physical consequence."""
        device_id = _normalize_device_id(device_id)
        devices = self.devices.read()
        for d in devices:
            if d["id"] == device_id:
                d["locked"] = False
                self.devices.write(devices)
                return d
        valid = [d.get("id") for d in devices]
        raise GuardianError(f"No device with id '{device_id}'. Valid ids: {valid}. Call get_devices first.")

    def has_reminder(self, reminder_id: Any) -> bool:
        return any(r.get("id") == reminder_id for r in self.reminders.read())

    def has_device(self, device_id: Any) -> bool:
        device_id = _normalize_device_id(device_id)
        return any(d.get("id") == device_id for d in self.devices.read())

    def valid_device_ids(self) -> list:
        return [d.get("id") for d in self.devices.read()]

    def _sweep_expired(self) -> int:
        """Expire overdue pendings now so they can't linger. Returns count swept."""
        now = datetime.datetime.now(datetime.timezone.utc)
        expired = [cid for cid, p in self._pending.items() if now > p["expires_at"]]
        for cid in expired:
            pending = self._pending.pop(cid)
            self._record(pending["tool"], pending["arguments"], RiskTier.HIGH, "expired")
        return len(expired)



    # Confirmation Gate
    def request_confirmation(self, tool: str, arguments: Dict[str, Any]) -> str:
        self._sweep_expired()
        # Normalize aliases before parking so confirm_action replays an exact id.
        arguments = dict(arguments or {})
        if tool == "unlock_smart_lock" and "device_id" in arguments:
            arguments["device_id"] = _normalize_device_id(arguments["device_id"])
        confirmation_id = str(uuid.uuid4())
        self._pending[confirmation_id] = {
            "tool": tool,
            "arguments": arguments,
            "expires_at": datetime.datetime.now(datetime.timezone.utc) + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
        }
        self._record(tool, arguments, RiskTier.HIGH, "pending_confirmation", {"confirmation_id": confirmation_id})
        return confirmation_id
 
    def confirm_action(self, confirmation_id: str) -> Any:
        self._sweep_expired()
        pending = self._pending.get(confirmation_id)
        if pending is None:
            raise GuardianError("Unknown, expired, or already-used confirmation id. Re-issue the original tool call.")
        if datetime.datetime.now(datetime.timezone.utc) > pending["expires_at"]:
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
        self._sweep_expired()
        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            raise GuardianError("Unknown, expired, or already-used confirmation id.")
        self._record(pending["tool"], pending["arguments"], RiskTier.HIGH, "cancelled")
        return {"cancelled": confirmation_id}

    def list_pending(self) -> list:
        """Non-expired staged HIGH-risk actions awaiting human decision."""
        self._sweep_expired()
        now = datetime.datetime.now(datetime.timezone.utc)
        out = []
        for cid, p in self._pending.items():
            remaining = int((p["expires_at"] - now).total_seconds())
            out.append({
                "confirmation_id": cid,
                "tool": p["tool"],
                "arguments": p["arguments"],
                "expires_in_seconds": max(0, remaining),
                "expires_at": p["expires_at"].isoformat().replace("+00:00", "Z"),
            })
        return out
