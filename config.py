"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Bot
BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
BOT_VERSION: str = os.getenv("BOT_VERSION", "1.0.0")

# Discord
EVENT_HOST_ROLE_ID: int | None = (
    int(os.getenv("EVENT_HOST_ROLE_ID")) if os.getenv("EVENT_HOST_ROLE_ID") else None
)
_ping_role = os.getenv("PING_ROLE_ID", "1521223466190377101")
PING_ROLE_ID: int | None = int(_ping_role) if _ping_role else None

# Database
DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", "signupbot.db"))

# Signup behaviour
MESSAGE_DELETE_DELAY_SECONDS: float = float(os.getenv("MESSAGE_DELETE_DELAY_SECONDS", "3"))
SIGNUP_COOLDOWN_SECONDS: float = float(os.getenv("SIGNUP_COOLDOWN_SECONDS", "2"))
# Your local timezone — start times you enter are interpreted in this zone.
EVENT_TIMEZONE: str = os.getenv("EVENT_TIMEZONE", "America/New_York")

# Albion Online
ALBION_GUILD_ID: str = os.getenv("ALBION_GUILD_ID", "").strip()
ALBION_REGION: str = os.getenv("ALBION_REGION", "americas").strip().lower()
ALBION_TIMEOUT_SECONDS: float = float(os.getenv("ALBION_TIMEOUT_SECONDS", "60"))

# Logging
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Embed colours (Discord colour integers)
EMBED_COLOUR_OPEN: int = 0x57F287   # Discord green
EMBED_COLOUR_CLOSED: int = 0xED4245  # Discord red

# AVA loot split sell-off deductions (percent lost when selling loot)
CITY_SELLOFF_DEDUCTION_PERCENT: float = float(
    os.getenv("CITY_SELLOFF_DEDUCTION_PERCENT", "15")
)
HIDEOUT_SELLOFF_DEDUCTION_PERCENT: float = float(
    os.getenv("HIDEOUT_SELLOFF_DEDUCTION_PERCENT", "20")
)
