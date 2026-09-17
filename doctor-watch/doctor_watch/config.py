from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

# .env 는 프로젝트 루트(doctor-watch/) 또는 현재 디렉터리에서 읽는다.
load_dotenv(PROJECT_DIR / ".env")
load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _path(name: str, default: str) -> Path:
    raw = os.environ.get(name, default)
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_DIR / p


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: _path("DOCTOR_WATCH_DB", "data/doctor_watch.db"))
    reports_dir: Path = field(default_factory=lambda: _path("DOCTOR_WATCH_REPORTS", "reports"))
    hira_service_key: str = field(default_factory=lambda: os.environ.get("HIRA_SERVICE_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("DOCTOR_WATCH_MODEL", "claude-opus-5"))
    concurrency: int = field(default_factory=lambda: _int("DOCTOR_WATCH_CONCURRENCY", 8))
    # 동시 Claude 호출 수 (요금제 rate limit 에 맞춰 조정)
    llm_concurrency: int = field(default_factory=lambda: _int("DOCTOR_WATCH_LLM_CONCURRENCY", 4))
    max_pages_per_hospital: int = field(default_factory=lambda: _int("DOCTOR_WATCH_MAX_PAGES", 200))
    max_chars_per_page: int = field(default_factory=lambda: _int("DOCTOR_WATCH_MAX_CHARS", 120_000))
    # 실행 1회당 Claude 호출 상한 (0 = 무제한). 초과하면 규칙 기반 추출로 대체된다.
    max_llm_calls_per_run: int = field(default_factory=lambda: _int("DOCTOR_WATCH_MAX_LLM_CALLS", 1500))
    request_timeout: float = 25.0
    per_host_delay: float = 0.6
    render_mode: str = field(default_factory=lambda: os.environ.get("DOCTOR_WATCH_RENDER", "auto"))
    # robots.txt 존중 여부 (전역). 병원별로는 hospitals.ignore_robots 로 예외 지정.
    respect_robots: bool = field(default_factory=lambda: os.environ.get("DOCTOR_WATCH_RESPECT_ROBOTS", "true").lower() not in ("0", "false", "no"))
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "DOCTOR_WATCH_USER_AGENT",
            "Mozilla/5.0 (compatible; doctor-watch/0.1; weekly medical-staff roster monitor)",
        )
    )
    # 알림
    slack_webhook_url: str = field(default_factory=lambda: os.environ.get("SLACK_WEBHOOK_URL", ""))
    telegram_bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    mail_from: str = field(default_factory=lambda: os.environ.get("MAIL_FROM", ""))
    mail_to: str = field(default_factory=lambda: os.environ.get("MAIL_TO", ""))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


settings = Settings()
