import hashlib
import hmac
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from leadbot.brain import BotTurn, BrainError, ClaudeBrain, ScriptedBrain
from leadbot.config import Settings
from leadbot.main import create_app, csv_safe
from leadbot.service import LeadService, score_lead
from leadbot.store import Store
from leadbot.whatsapp import CloudApiSender, Inbound, parse_inbound, verify_signature

PROFILE = {
    "name": "Test Clinic",
    "services": {"Cleaning": "Rs 1,500", "Root canal": "After checkup"},
    "service_keywords": {"Cleaning": ["cleaning"], "Root canal": ["root canal"]},
}
SECRET = "app-secret"


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.texts, self.alerts = [], []

    async def send_text(self, to, body):
        self.texts.append((to, body))
        return self.ok

    async def send_alert(self, to, body):
        self.alerts.append((to, body))
        return self.ok


class FixedBrain:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = 0

    def next_turn(self, profile, lead, history):
        self.calls += 1
        return self.turns.pop(0) if self.turns else BotTurn(reply="ok")


class BrokenBrain:
    def next_turn(self, profile, lead, history):
        raise BrainError("down")


def make_settings(tmp_path, **overrides):
    values = dict(
        demo_mode=True,
        database_path=str(tmp_path / "t.db"),
        owner_phone="910000000000",
        app_secret=SECRET,
        verify_token="verify-me",
        profile=PROFILE,
    )
    values.update(overrides)
    return Settings(**values)


def make_service(tmp_path, brain, sender=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    store = Store(settings.database_path)
    sender = sender or FakeSender()
    return LeadService(settings, store, brain, sender), store, sender


def text(message_id, body="hello", phone="911111111111"):
    return Inbound(message_id, phone, "text", body, "Asha")


COMPLETE = BotTurn(
    reply="Thanks", name="Asha", service="Cleaning", urgency="this_week", preferred_time="Friday evening"
)


def webhook_payload(message_id="wamid.1", body="Hi"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"profile": {"name": "Asha"}, "wa_id": "911111111111"}],
            "messages": [{"from": "911111111111", "id": message_id, "timestamp": "1", "type": "text",
                          "text": {"body": body}}],
        }}]}],
    }


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


# Webhook parsing and security

def test_signature_checks():
    raw = b'{"a":1}'
    assert verify_signature(SECRET, raw, sign(raw))
    assert not verify_signature(SECRET, raw + b" ", sign(raw))
    assert not verify_signature(SECRET, raw, None)
    assert not verify_signature("", raw, sign(raw))
    assert not verify_signature(SECRET, raw, "sha1=abc")


def test_parse_text_status_and_media():
    assert parse_inbound(webhook_payload())[0].text == "Hi"
    assert parse_inbound(webhook_payload())[0].profile_name == "Asha"
    status_only = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert parse_inbound(status_only) == []
    media = {"entry": [{"changes": [{"value": {"messages": [{"id": "m", "from": "1", "type": "image"}]}}]}]}
    assert parse_inbound(media)[0].kind == "image"
    assert parse_inbound({}) == []


def test_webhook_verification_and_rejection(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path), FixedBrain(), FakeSender()))
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"})
    assert bad.status_code == 403
    assert client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403


def test_signed_webhook_replies_and_retry_is_ignored(tmp_path):
    brain, sender = FixedBrain(BotTurn(reply="Hello!")), FakeSender()
    client = TestClient(create_app(make_settings(tmp_path), brain, sender))
    raw = json.dumps(webhook_payload()).encode()
    headers = {"X-Hub-Signature-256": sign(raw)}
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert brain.calls == 1
    assert sender.texts == [("911111111111", "Hello!")]


def test_live_mode_refuses_missing_settings(tmp_path):
    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        create_app(make_settings(tmp_path, demo_mode=False), FixedBrain(), FakeSender())


def test_live_mode_locks_dashboard_and_disables_simulator(tmp_path):
    settings = make_settings(
        tmp_path, demo_mode=False, access_token="t", phone_number_id="1",
        anthropic_api_key="k", admin_password="pw",
    )
    client = TestClient(create_app(settings, FixedBrain(), FakeSender()))
    assert client.get("/").status_code == 401
    assert client.get("/api/state").status_code == 401
    assert client.get("/leads.csv").status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/", auth=("admin", "pw")).status_code == 200
    assert client.post("/demo/message", json={"phone": "911111111111", "text": "x"}, auth=("admin", "pw")).status_code == 404
    assert client.post("/demo/reset", auth=("admin", "pw")).status_code == 404


# Conversation flow

def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_qualified_lead_alerts_once_and_syncs_crm(tmp_path):
    posted = []

    async def crm(payload):
        posted.append(payload)
        return True

    service, store, sender = make_service(tmp_path, FixedBrain(COMPLETE, BotTurn(reply="More")), lead_webhook_url="https://x.test")
    service._crm_post = crm
    run(service.handle(text("1")))
    run(service.handle(text("2")))
    lead = store.leads()[0]
    assert lead["status"] == "qualified" and lead["score"] == "hot" and lead["crm_synced"] == 1
    assert len(sender.alerts) == 1 and "Asha" in sender.alerts[0][1] and "Friday evening" in sender.alerts[0][1]
    assert len(posted) == 1 and posted[0]["service"] == "Cleaning"


def test_failed_alert_is_recorded(tmp_path):
    sender = FakeSender(ok=False)
    service, store, _ = make_service(tmp_path, FixedBrain(COMPLETE), sender)
    run(service.handle(text("1")))
    assert store.leads()[0]["alert_sent"] == 0
    assert store.alerts()[0]["delivered"] == 0
    assert store.history("911111111111")[-1]["delivery"] == "failed"


def test_human_handoff_stops_bot(tmp_path):
    brain = FixedBrain(BotTurn(reply="Team will call", wants_human=True))
    service, store, sender = make_service(tmp_path, brain)
    run(service.handle(text("1", "call me")))
    run(service.handle(text("2", "hello?")))
    assert store.leads()[0]["status"] == "needs_human"
    assert brain.calls == 1 and len(sender.texts) == 1 and len(sender.alerts) == 1


def test_qualified_lead_asking_for_person_alerts_again(tmp_path):
    brain = FixedBrain(COMPLETE, BotTurn(reply="Team will call", wants_human=True))
    service, store, sender = make_service(tmp_path, brain)
    run(service.handle(text("1")))
    run(service.handle(text("2", "call me")))
    assert store.leads()[0]["status"] == "needs_human"
    assert len(sender.alerts) == 2 and "asked for a person" in sender.alerts[1][1]


def test_opt_out_and_back_in(tmp_path):
    brain = FixedBrain()
    service, store, sender = make_service(tmp_path, brain)
    run(service.handle(text("1", "STOP")))
    run(service.handle(text("2", "hello")))
    assert store.leads()[0]["status"] == "opted_out" and brain.calls == 0 and len(sender.texts) == 1
    run(service.handle(text("3", "start")))
    assert store.leads()[0]["status"] == "qualifying" and len(sender.texts) == 2


def test_brain_failure_hands_off(tmp_path):
    service, store, sender = make_service(tmp_path, BrokenBrain())
    run(service.handle(text("1")))
    assert store.leads()[0]["status"] == "needs_human"
    assert "team" in sender.texts[0][1] and len(sender.alerts) == 1


def test_non_text_gets_prompt_without_brain(tmp_path):
    brain = FixedBrain()
    service, _, sender = make_service(tmp_path, brain)
    run(service.handle(Inbound("1", "911111111111", "image")))
    assert brain.calls == 0 and "text" in sender.texts[0][1]


def test_turn_limit_hands_off(tmp_path):
    brain = FixedBrain()
    service, store, sender = make_service(tmp_path, brain, max_bot_turns=2)
    for i in range(3):
        run(service.handle(text(str(i))))
    assert brain.calls == 2 and store.leads()[0]["status"] == "needs_human" and len(sender.alerts) == 1


def test_model_cannot_invent_service_or_erase_details(tmp_path):
    brain = FixedBrain(
        BotTurn(reply="a", service="Cleaning", name="Asha\nBAD"),
        BotTurn(reply="b", service="Free iPhone"),
    )
    service, store, _ = make_service(tmp_path, brain)
    run(service.handle(text("1")))
    run(service.handle(text("2")))
    lead = store.leads()[0]
    assert lead["service"] == "Cleaning" and lead["name"] == "Asha BAD"


def test_scoring():
    assert score_lead({"service": "x", "urgency": "today"}) == "hot"
    assert score_lead({"service": "x", "urgency": "this_month"}) == "warm"
    assert score_lead({"service": "x", "urgency": "exploring"}) == "cold"
    assert score_lead({"urgency": "today"}) == "cold"


# Scripted brain

def scripted(history, **lead):
    return ScriptedBrain().next_turn(PROFILE, lead, history)


def test_scripted_full_conversation(tmp_path):
    service, store, sender = make_service(tmp_path, ScriptedBrain())
    for i, message in enumerate(["Hi, how much is cleaning?", "this week", "Friday evening", "Asha Sharma"]):
        run(service.handle(text(str(i), message)))
    lead = store.leads()[0]
    assert lead["service"] == "Cleaning" and lead["urgency"] == "this_week"
    assert lead["preferred_time"] == "Friday evening" and lead["name"] == "Asha Sharma"
    assert lead["status"] == "qualified" and lead["score"] == "hot"
    assert "Rs 1,500" in sender.texts[0][1]


def test_scripted_hinglish():
    turn = scripted([{"direction": "in", "body": "Mujhe root canal karwana hai, kal shaam ko"}])
    assert turn.service == "Root canal" and turn.urgency == "this_week"
    assert turn.preferred_time == "kal shaam"
    assert "naam" in turn.reply


def test_scripted_human_request():
    assert scripted([{"direction": "in", "body": "Can I talk to a real person?"}]).wants_human


# Claude brain

class FakeBeta:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None
        self.beta = SimpleNamespace(messages=self)

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


HISTORY = [
    {"direction": "out", "body": "old bot message"},
    {"direction": "in", "body": "Hi"},
    {"direction": "out", "body": "Which service?"},
    {"direction": "in", "body": "Cleaning"},
]


def test_claude_request_shape():
    fake = FakeBeta(SimpleNamespace(stop_reason="end_turn", parsed_output=BotTurn(reply="Hello")))
    turn = ClaudeBrain("k", client=fake).next_turn(PROFILE, {"service": "Cleaning"}, HISTORY)
    assert turn.reply == "Hello"
    kwargs = fake.kwargs
    assert kwargs["model"] == "claude-opus-5-5" and kwargs["output_format"] is BotTurn
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["messages"][0] == {"role": "user", "content": "Hi"}
    assert "Test Clinic" in kwargs["system"] and '"service": "Cleaning"' in kwargs["system"]
    assert "service_keywords" not in kwargs["system"]


@pytest.mark.parametrize("response", [
    SimpleNamespace(stop_reason="refusal", parsed_output=None),
    SimpleNamespace(stop_reason="max_tokens", parsed_output=None),
    SimpleNamespace(stop_reason="end_turn", parsed_output=BotTurn(reply="  ")),
])
def test_claude_bad_responses_raise(response):
    with pytest.raises(BrainError):
        ClaudeBrain("k", client=FakeBeta(response)).next_turn(PROFILE, {}, HISTORY)


def test_claude_api_error_raises():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    with pytest.raises(BrainError):
        ClaudeBrain("k", client=FakeBeta(error=error)).next_turn(PROFILE, {}, HISTORY)


# Sending and export

def test_cloud_sender_payloads():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    sender = CloudApiSender("tok", "123", "v23.0", "lead_alert", "en", httpx.MockTransport(handler))
    assert run(sender.send_text("91", "hello"))
    assert run(sender.send_alert("91", "line one\nline two"))
    assert seen[0][0] == "https://graph.facebook.com/v23.0/123/messages"
    assert seen[0][1] == "Bearer tok"
    assert seen[0][2]["text"]["body"] == "hello"
    params = seen[1][2]["template"]["components"][0]["parameters"]
    assert seen[1][2]["template"]["name"] == "lead_alert" and params[0]["text"] == "line one | line two"


def test_cloud_sender_reports_rejection():
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {}}))
    assert not run(CloudApiSender("t", "1", transport=transport).send_text("91", "x"))


def test_csv_blocks_formulas(tmp_path):
    assert csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and csv_safe("Asha") == "Asha" and csv_safe(None) == ""
    client = TestClient(create_app(make_settings(tmp_path), FixedBrain(BotTurn(reply="a", name="=cmd()")), FakeSender()))
    client.post("/demo/message", json={"phone": "911111111111", "text": "hi"})
    assert "'=cmd()" in client.get("/leads.csv").text


def test_simulator_and_state(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path), FixedBrain(BotTurn(reply="Hello")), FakeSender()))
    assert client.post("/demo/message", json={"phone": "bad", "text": "x"}).status_code == 422
    result = client.post("/demo/message", json={"phone": "911111111111", "text": "hi"}).json()
    assert [m["direction"] for m in result["messages"]] == ["in", "out"]
    assert len(client.get("/api/state").json()["leads"]) == 1
    assert client.post("/demo/reset").status_code == 200
    assert client.get("/api/state").json()["leads"] == []
    assert client.get("/").status_code == 200
