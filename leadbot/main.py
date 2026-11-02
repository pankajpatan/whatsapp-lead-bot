"""FastAPI app: WhatsApp webhook, demo simulator, dashboard and CSV export."""
from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .brain import Brain, ClaudeBrain, ScriptedBrain
from .config import Settings
from .service import LeadService
from .store import Store
from .whatsapp import CloudApiSender, DemoSender, Inbound, Sender, parse_inbound, verify_signature

log = logging.getLogger(__name__)
PAGE = Path(__file__).parent / "static" / "index.html"
basic = HTTPBasic(auto_error=False)
CSV_COLUMNS = ("phone", "name", "service", "urgency", "preferred_time", "score", "status", "crm_synced")


class SimulatedMessage(BaseModel):
    phone: str = Field(pattern=r"^\d{6,15}$")
    text: str = Field(min_length=1, max_length=2000)


def csv_safe(value: object) -> str:
    """Stop customer text from running as a formula when the file opens in Excel or Sheets."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def create_app(
    settings: Settings | None = None,
    brain: Brain | None = None,
    sender: Sender | None = None,
) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
    settings.validate_live()

    if brain is None:
        brain = (
            ClaudeBrain(settings.anthropic_api_key, settings.model)
            if settings.anthropic_api_key
            else ScriptedBrain()
        )
    if sender is None:
        sender = (
            DemoSender()
            if settings.demo_mode
            else CloudApiSender(
                settings.access_token,
                settings.phone_number_id,
                settings.api_version,
                settings.owner_template,
                settings.owner_template_lang,
            )
        )

    store = Store(settings.database_path)
    service = LeadService(settings, store, brain, sender)
    app = FastAPI(title="WhatsApp Lead Bot", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.service = service

    def require_admin(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if settings.demo_mode:
            return
        valid = credentials is not None and secrets.compare_digest(
            credentials.password.encode(), settings.admin_password.encode()
        )
        if not valid:
            raise HTTPException(401, "Authentication required", {"WWW-Authenticate": "Basic"})

    def require_demo() -> None:
        if not settings.demo_mode:
            raise HTTPException(404, "Not found")

    @app.get("/webhook")
    async def verify_webhook(request: Request) -> PlainTextResponse:
        query = request.query_params
        token = query.get("hub.verify_token", "")
        if (
            query.get("hub.mode") == "subscribe"
            and settings.verify_token
            and secrets.compare_digest(token.encode(), settings.verify_token.encode())
        ):
            return PlainTextResponse(query.get("hub.challenge", ""))
        raise HTTPException(403, "Verification failed")

    @app.post("/webhook")
    async def receive_webhook(request: Request, background: BackgroundTasks) -> dict:
        raw = await request.body()
        if not verify_signature(settings.app_secret, raw, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(403, "Invalid signature")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "Invalid JSON") from exc
        # Reply to Meta first and do the slow work afterwards, or Meta will retry the webhook.
        for inbound in parse_inbound(payload):
            background.add_task(service.handle, inbound)
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    async def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"))

    @app.get("/api/state", dependencies=[Depends(require_admin)])
    async def state(phone: str | None = None) -> dict:
        return {
            "business": settings.profile.get("name"),
            "demo_mode": settings.demo_mode,
            "brain": type(brain).__name__,
            "leads": store.leads(),
            "alerts": store.alerts(),
            "messages": store.history(phone, 100) if phone else [],
        }

    @app.get("/leads.csv", dependencies=[Depends(require_admin)])
    async def leads_csv() -> Response:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for lead in store.leads():
            writer.writerow(csv_safe(lead.get(column)) for column in CSV_COLUMNS)
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
        )

    @app.post("/demo/message", dependencies=[Depends(require_demo)])
    async def demo_message(message: SimulatedMessage) -> dict:
        await service.handle(
            Inbound(f"demo-{uuid.uuid4().hex}", message.phone, "text", message.text, "Demo customer")
        )
        return {"messages": store.history(message.phone, 100)}

    @app.post("/demo/reset", dependencies=[Depends(require_demo)])
    async def demo_reset() -> dict:
        store.reset()
        return {"status": "ok"}

    return app
