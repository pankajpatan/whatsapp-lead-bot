"""Settings, read from the environment."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    demo_mode: bool = True
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    access_token: str = ""
    phone_number_id: str = ""
    app_secret: str = ""
    verify_token: str = ""
    api_version: str = "v23.0"
    admin_password: str = ""
    owner_phone: str = ""
    owner_template: str = ""
    owner_template_lang: str = "en"
    lead_webhook_url: str = ""
    database_path: str = "leadbot.db"
    business_profile: str = "business_profile.json"
    max_bot_turns: int = 12
    profile: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        get = os.environ.get
        settings = cls(
            demo_mode=get("DEMO_MODE", "true").strip().lower() != "false",
            anthropic_api_key=get("ANTHROPIC_API_KEY", ""),
            model=get("LEAD_BOT_MODEL", "claude-opus-5-5"),
            access_token=get("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=get("WHATSAPP_PHONE_NUMBER_ID", ""),
            app_secret=get("WHATSAPP_APP_SECRET", ""),
            verify_token=get("WHATSAPP_VERIFY_TOKEN", ""),
            api_version=get("WHATSAPP_API_VERSION", "v23.0"),
            admin_password=get("ADMIN_PASSWORD", ""),
            owner_phone=get("OWNER_PHONE", ""),
            owner_template=get("OWNER_ALERT_TEMPLATE", ""),
            owner_template_lang=get("OWNER_ALERT_TEMPLATE_LANG", "en"),
            lead_webhook_url=get("LEAD_WEBHOOK_URL", ""),
            database_path=get("DATABASE_PATH", "leadbot.db"),
            business_profile=get("BUSINESS_PROFILE", "business_profile.json"),
            max_bot_turns=int(get("MAX_BOT_TURNS", "12")),
        )
        settings.load_profile()
        return settings

    def load_profile(self) -> None:
        path = Path(self.business_profile)
        if not path.is_absolute():
            path = ROOT / path
        self.profile = json.loads(path.read_text(encoding="utf-8"))

    def validate_live(self) -> None:
        """Refuse to start a live server with a missing security setting."""
        if self.demo_mode:
            return
        required = {
            "WHATSAPP_ACCESS_TOKEN": self.access_token,
            "WHATSAPP_PHONE_NUMBER_ID": self.phone_number_id,
            "WHATSAPP_APP_SECRET": self.app_secret,
            "WHATSAPP_VERIFY_TOKEN": self.verify_token,
            "ADMIN_PASSWORD": self.admin_password,
            "OWNER_PHONE": self.owner_phone,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Missing settings for live mode: " + ", ".join(missing))
