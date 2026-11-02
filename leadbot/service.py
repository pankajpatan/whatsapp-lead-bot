"""The conversation flow: one inbound message to reply, lead update and alerts."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from typing import Awaitable, Callable

import httpx

from .brain import BotTurn, Brain, BrainError
from .config import Settings
from .store import Store
from .whatsapp import Inbound, Sender

log = logging.getLogger(__name__)

MAX_INBOUND_CHARS = 2000
REQUIRED = ("service", "urgency", "preferred_time", "name")
OPT_OUT = {"stop", "unsubscribe", "band karo", "message mat karo"}
OPT_IN = {"start"}
FAILURE_REPLY = "Thanks for your message. Our team will get back to you shortly."
NON_TEXT_REPLY = "I can only read text messages right now. Please type your question."
HANDOFF_REPLY = "Thanks for your patience. Our team will contact you shortly."
OPT_OUT_REPLY = "You won't get more messages from us. Reply START if you change your mind."
OPT_IN_REPLY = "Welcome back. How can we help you?"

_UNSAFE = re.compile(r"[\r\n\t]+")


def score_lead(lead: dict) -> str:
    """Scored in code, so the customer's wording cannot talk the bot into a higher score."""
    urgency = lead.get("urgency")
    if lead.get("service") and urgency in ("today", "this_week"):
        return "hot"
    if lead.get("service") and urgency == "this_month":
        return "warm"
    if lead.get("service") and urgency is None:
        return "warm"
    return "cold"


def _clean(value: str | None, limit: int = 120) -> str | None:
    if value is None:
        return None
    value = _UNSAFE.sub(" ", value).strip()
    return value[:limit] or None


class LeadService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        brain: Brain,
        sender: Sender,
        crm_post: Callable[[dict], Awaitable[bool]] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.brain = brain
        self.sender = sender
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._crm_post = crm_post or self._post_webhook

    async def handle(self, inbound: Inbound) -> None:
        body = inbound.text if inbound.kind == "text" else f"[{inbound.kind} message]"
        body = body[:MAX_INBOUND_CHARS]
        # Meta retries webhooks, so a repeated message ID must never trigger a second reply.
        if not self.store.add_inbound(inbound.phone, body, inbound.message_id):
            return
        async with self._locks[inbound.phone]:
            await self._process(inbound, body)

    async def _process(self, inbound: Inbound, body: str) -> None:
        phone = inbound.phone
        lead = self.store.get_or_create_lead(phone, inbound.profile_name)
        command = body.strip().lower()

        if lead["status"] == "opted_out":
            if command in OPT_IN:
                self.store.update_lead(phone, status="qualifying")
                await self._reply(phone, OPT_IN_REPLY)
            return
        if command in OPT_OUT:
            self.store.update_lead(phone, status="opted_out")
            await self._reply(phone, OPT_OUT_REPLY)
            return
        if lead["status"] == "needs_human":
            return  # A person has taken over, so the bot stays quiet.
        if inbound.kind != "text" or not body.strip():
            await self._reply(phone, NON_TEXT_REPLY)
            return
        if lead["bot_turns"] >= self.settings.max_bot_turns:
            await self._hand_off(phone, lead, "conversation reached the bot's turn limit", HANDOFF_REPLY)
            return

        history = self.store.history(phone)
        try:
            turn = await asyncio.to_thread(self.brain.next_turn, self.settings.profile, lead, history)
        except BrainError as exc:
            log.error("Brain failed: %s", exc)
            await self._hand_off(phone, lead, "the bot could not answer", FAILURE_REPLY)
            return
        except Exception:
            log.exception("Unexpected brain failure")
            await self._hand_off(phone, lead, "the bot could not answer", FAILURE_REPLY)
            return

        lead = self._merge(phone, lead, turn)
        if turn.wants_human:
            await self._hand_off(phone, lead, "customer asked for a person", turn.reply)
            return
        await self._reply(phone, turn.reply)
        if all(lead.get(field) for field in REQUIRED) and lead["status"] == "qualifying":
            self.store.update_lead(phone, status="qualified")
            lead["status"] = "qualified"
            await self._alert_once(phone, lead, "new qualified lead")
            await self._sync_crm(phone, lead)

    def _merge(self, phone: str, lead: dict, turn: BotTurn) -> dict:
        changes: dict = {"bot_turns": lead["bot_turns"] + 1}
        services = self.settings.profile.get("services", {})
        if turn.service in services:
            changes["service"] = turn.service
        if turn.name:
            changes["name"] = _clean(turn.name, 60)
        if turn.urgency:
            changes["urgency"] = turn.urgency
        if turn.preferred_time:
            changes["preferred_time"] = _clean(turn.preferred_time)
        merged = {**lead, **changes}
        changes["score"] = merged["score"] = score_lead(merged)
        self.store.update_lead(phone, **changes)
        return merged

    async def _reply(self, phone: str, text: str) -> None:
        delivered = await self.sender.send_text(phone, text)
        self.store.add_outbound(phone, text, delivered)

    async def _hand_off(self, phone: str, lead: dict, reason: str, reply: str) -> None:
        self.store.update_lead(phone, status="needs_human")
        await self._reply(phone, reply)
        # A handoff happens once per lead and always needs the owner's attention.
        await self._alert(phone, lead, reason)

    async def _alert_once(self, phone: str, lead: dict, reason: str) -> None:
        if not lead.get("alert_sent"):
            await self._alert(phone, lead, reason)

    async def _alert(self, phone: str, lead: dict, reason: str) -> None:
        lines = [
            f"{self.settings.profile.get('name', 'Lead bot')}: {reason}",
            f"Customer: {lead.get('name') or lead.get('profile_name') or 'unknown'} (+{phone})",
            f"Service: {lead.get('service') or 'not given'}",
            f"When: {lead.get('preferred_time') or 'not given'}",
            f"Score: {lead.get('score', 'cold')}",
        ]
        body = "\n".join(lines)
        delivered = False
        if self.settings.owner_phone or self.settings.demo_mode:
            delivered = await self.sender.send_alert(self.settings.owner_phone, body)
        self.store.add_alert(phone, body, delivered)
        if delivered:
            self.store.update_lead(phone, alert_sent=1)
        else:
            log.error("Owner alert not delivered")

    async def _sync_crm(self, phone: str, lead: dict) -> None:
        if not self.settings.lead_webhook_url:
            return
        payload = {
            "phone": phone,
            **{k: lead.get(k) for k in ("name", "service", "urgency", "preferred_time", "score", "status")},
        }
        if await self._crm_post(payload):
            self.store.update_lead(phone, crm_synced=1)

    async def _post_webhook(self, payload: dict) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(self.settings.lead_webhook_url, json=payload)
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("CRM webhook failed: %s", type(exc).__name__)
            return False
