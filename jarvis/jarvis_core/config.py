"""Configuration from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
INGRESS_GATEWAY = "172.30.32.2"


def _env(env: dict[str, str], key: str, default: str = "") -> str:
    value = env.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on", "ja")


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class MailConfig:
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"  # ssl | starttls
    user: str = ""
    password: str = ""
    sender: str = ""
    drafts_folder: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.imap_host and self.user and self.password)

    @property
    def can_send(self) -> bool:
        return self.configured and bool(self.smtp_host)


@dataclass
class HAConfig:
    base_url: str = ""  # ends with /api
    token: str = ""
    trusted_entities: tuple[str, ...] = ()  # scenes/scripts that may run without approval

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)


@dataclass
class Config:
    anthropic_api_key: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_tokens: int = 32000
    max_steps: int = 12
    fallbacks: bool = True
    web_search: bool = True
    timezone: str = "Europe/Berlin"
    user_name: str = ""
    data_dir: Path = field(default_factory=lambda: Path("data"))
    web_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "web")
    host: str = "127.0.0.1"
    port: int = 8099
    token: str = ""
    addon: bool = False
    todoist_token: str = ""
    mail: MailConfig = field(default_factory=MailConfig)
    ha: HAConfig = field(default_factory=HAConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.sqlite3"

    @property
    def access_mode(self) -> str:
        """`ingress` (add-on), `token` or `local` (loopback only)."""
        if self.addon:
            return "ingress"
        if self.token:
            return "token"
        return "local"

    @property
    def bind_host(self) -> str:
        if self.access_mode == "local":
            return "127.0.0.1"
        return self.host or "0.0.0.0"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        supervisor_token = _env(env, "SUPERVISOR_TOKEN")
        addon = _bool(_env(env, "JARVIS_ADDON"), default=bool(supervisor_token))

        effort = _env(env, "JARVIS_EFFORT", "medium").lower()
        if effort not in EFFORT_LEVELS:
            effort = "medium"

        if supervisor_token:
            ha = HAConfig(base_url="http://supervisor/core/api", token=supervisor_token)
        else:
            url = _env(env, "HA_URL").rstrip("/")
            if url and not url.endswith("/api"):
                url = url + "/api"
            ha = HAConfig(base_url=url, token=_env(env, "HA_TOKEN"))
        ha.trusted_entities = tuple(
            e.strip().lower() for e in re.split(r"[,\s]+", _env(env, "HA_TRUSTED_ENTITIES")) if e.strip()
        )

        user = _env(env, "MAIL_USER")
        mail = MailConfig(
            imap_host=_env(env, "MAIL_IMAP_HOST"),
            imap_port=_int(_env(env, "MAIL_IMAP_PORT"), 993),
            smtp_host=_env(env, "MAIL_SMTP_HOST"),
            smtp_port=_int(_env(env, "MAIL_SMTP_PORT"), 465),
            smtp_security=(_env(env, "MAIL_SMTP_SECURITY", "ssl").lower() or "ssl"),
            user=user,
            password=env.get("MAIL_PASSWORD", ""),
            sender=_env(env, "MAIL_FROM") or user,
            drafts_folder=_env(env, "MAIL_DRAFTS_FOLDER"),
        )

        cfg = cls(
            anthropic_api_key=_env(env, "ANTHROPIC_API_KEY"),
            model=_env(env, "JARVIS_MODEL") or DEFAULT_MODEL,
            effort=effort,
            max_tokens=_int(_env(env, "JARVIS_MAX_TOKENS"), 32000),
            max_steps=_int(_env(env, "JARVIS_MAX_STEPS"), 12),
            fallbacks=_bool(_env(env, "JARVIS_FALLBACKS"), True),
            web_search=_bool(_env(env, "JARVIS_WEB_SEARCH"), True),
            timezone=_env(env, "JARVIS_TIMEZONE") or "Europe/Berlin",
            user_name=_env(env, "JARVIS_USER_NAME"),
            data_dir=Path(_env(env, "JARVIS_DATA_DIR") or ("/data" if addon else "data")),
            host=_env(env, "JARVIS_HOST"),
            port=_int(_env(env, "JARVIS_PORT"), 8099),
            token=_env(env, "JARVIS_TOKEN"),
            addon=addon,
            todoist_token=_env(env, "TODOIST_API_TOKEN"),
            mail=mail,
            ha=ha,
        )
        web_dir = _env(env, "JARVIS_WEB_DIR")
        if web_dir:
            cfg.web_dir = Path(web_dir)
        return cfg
