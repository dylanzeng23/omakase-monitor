from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Restaurant:
    name: str
    omakase_code: str
    tabelog_rating: float = 0.0
    tabelog_url: str = ""
    cuisine: str = ""
    location: str = "Tokyo"
    notes: str = ""


@dataclass
class AvailabilitySlot:
    omakase_code: str
    restaurant_name: str
    slot_date: str  # '2026-05-04'
    slot_time: str = ""  # '18:00' or empty
    course_name: str = ""
    price_jpy: int = 0
    status: str = "available"  # 'available', 'pending', 'unknown'

    @property
    def dedup_key(self) -> str:
        return f"{self.omakase_code}:{self.slot_date}:{self.slot_time}:{self.course_name}"


@dataclass
class RunLog:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    restaurants_checked: int = 0
    slots_found: int = 0
    new_slots: int = 0
    error_message: str = ""


@dataclass
class Config:
    omakase_email: str
    omakase_password: str
    target_dates: list[str]
    bot_token: str
    chat_id: str
    min_tabelog_rating: float = 4.3
    interval_minutes: float = 15.0
    min_delay_seconds: float = 15.0
    max_delay_seconds: float = 25.0
    headless: bool = True

    @classmethod
    def from_yaml(cls, data: dict) -> "Config":
        omakase = data.get("omakase", {})
        tg = data.get("telegram", {})
        sched = data.get("schedule", {})
        browser = data.get("browser", {})
        return cls(
            omakase_email=omakase.get("email", ""),
            omakase_password=omakase.get("password", ""),
            target_dates=data.get("target_dates", []),
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            min_tabelog_rating=data.get("min_tabelog_rating", 4.3),
            interval_minutes=sched.get("interval_minutes", 15.0),
            min_delay_seconds=sched.get("min_delay_seconds", 15.0),
            max_delay_seconds=sched.get("max_delay_seconds", 25.0),
            headless=browser.get("headless", True),
        )
