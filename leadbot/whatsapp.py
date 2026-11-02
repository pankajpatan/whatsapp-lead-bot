"""WhatsApp Cloud API: webhook parsing, signature checks and sending."""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)
MAX_TEXT = 4096


@dataclass
class Inbound:
    message_id: str
    phone: str
    kind: str
    text: str = ""
    profile_name: str | None = None


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header against the raw request body."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_inbound(payload: dict[str, Any]) -> list[Inbound]:
    """Extract customer messages from a webhook. Delivery statuses are ignored."""
    found: list[Inbound] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                message_id, phone = message.get("id"), message.get("from")
                if not message_id or not phone:
                    continue
                kind = message.get("type") or "unknown"
                text = ""
                if kind == "text":
                    text = (message.get("text") or {}).get("body") or ""
                elif kind == "button":
                    text = (message.get("button") or {}).get("text") or ""
                    kind = "text"
                elif kind == "interactive":
                    interactive = message.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    text = reply.get("title") or ""
                    kind = "text"
                found.append(Inbound(message_id, phone, kind, text, names.get(phone)))
    return found


class Sender(Protocol):
    async def send_text(self, to: str, body: str) -> bool: ...
    async def send_alert(self, to: str, body: str) -> bool: ...


class DemoSender:
    """Sends nothing. The simulator reads replies from the database."""

    async def send_text(self, to: str, body: str) -> bool:
        return True

    async def send_alert(self, to: str, body: str) -> bool:
        return True


class CloudApiSender:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v23.0",
        alert_template: str = "",
        alert_template_lang: str = "en",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._template = alert_template
        self._template_lang = alert_template_lang
        self._transport = transport

    async def _post(self, payload: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.post(self._url, headers=self._headers, json=payload)
        except httpx.HTTPError as exc:
            log.error("WhatsApp request failed: %s", type(exc).__name__)
            return False
        if response.status_code >= 400:
            # Log the status only: error bodies can repeat customer data.
            log.error("WhatsApp send rejected with HTTP %s", response.status_code)
            return False
        return True

    async def send_text(self, to: str, body: str) -> bool:
        return await self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body[:MAX_TEXT]},
        })

    async def send_alert(self, to: str, body: str) -> bool:
        if not self._template:
            return await self.send_text(to, body)
        # Template variables cannot contain line breaks or long runs of spaces.
        flat = " | ".join(line.strip() for line in body.splitlines() if line.strip())
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": self._template,
                "language": {"code": self._template_lang},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": flat[:1024]}]}
                ],
            },
        })
