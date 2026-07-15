"""Shared utilities for parsing, permissions, and embed rendering."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

import config
from models import FarmEvent

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

SLOT_LINE_PATTERN = re.compile(r"^(\d+)\s+(.+)$")
SIGNUP_MESSAGE_PATTERN = re.compile(r"^-?(\d+)$")

# Standard fame farm roster (11 slots).
DEFAULT_FARM_SLOTS: list[tuple[int, str]] = [
    (1, "Tank"),
    (2, "Main Heal"),
    (3, "Off Tank"),
    (4, "Cobra"),
    (5, "Iron Root"),
    (6, "DPS"),
    (7, "DPS"),
    (8, "DPS"),
    (9, "DPS"),
    (10, "DPS"),
    (11, "Scout"),
]


def get_default_farm_slots() -> list[tuple[int, str]]:
    """Return a copy of the standard farm slot layout."""
    return list(DEFAULT_FARM_SLOTS)


def parse_slot_configuration(text: str) -> list[tuple[int, str]]:
    """Parse multiline slot configuration into ordered (number, role) pairs."""
    slots: list[tuple[int, str]] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        match = SLOT_LINE_PATTERN.match(line)
        if not match:
            raise ValueError(
                f'Invalid slot line: "{line}". Expected format like "1 Tank".'
            )
        slots.append((int(match.group(1)), match.group(2).strip()))

    if not slots:
        raise ValueError("Slot configuration must include at least one slot.")

    numbers = [number for number, _ in slots]
    if len(numbers) != len(set(numbers)):
        raise ValueError("Slot configuration contains duplicate slot numbers.")

    return sorted(slots, key=lambda item: item[0])


def get_event_timezone() -> ZoneInfo:
    """Return the configured event timezone."""
    try:
        return ZoneInfo(config.EVENT_TIMEZONE)
    except Exception as exc:
        raise ValueError(
            f'Invalid EVENT_TIMEZONE "{config.EVENT_TIMEZONE}". '
            "Set it in .env — example: America/New_York"
        ) from exc


def parse_farm_date(text: str) -> tuple[int, int, int]:
    """Parse MM/DD/YYYY or YYYY-MM-DD into (year, month, day)."""
    text = text.strip()
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    else:
        us = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
        if not us:
            raise ValueError(
                f'Invalid date "{text}". Use **MM/DD/YYYY** — example: `6/29/2026`'
            )
        month, day, year = int(us.group(1)), int(us.group(2)), int(us.group(3))

    try:
        datetime(year, month, day)
    except ValueError as exc:
        raise ValueError(f'Invalid date "{text}".') from exc

    return year, month, day


def parse_farm_time(text: str) -> tuple[int, int]:
    """Parse 1:00 PM or 13:00 into (hour, minute)."""
    text = text.strip()
    twelve = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", text, re.I)
    if twelve:
        hour, minute = int(twelve.group(1)), int(twelve.group(2))
        meridiem = twelve.group(3).upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        elif meridiem == "AM" and hour == 12:
            hour = 0
    else:
        twenty_four = re.match(r"^(\d{1,2}):(\d{2})$", text)
        if not twenty_four:
            raise ValueError(
                f'Invalid time "{text}". Use **1:00 PM** or **13:00** (24-hour).'
            )
        hour, minute = int(twenty_four.group(1)), int(twenty_four.group(2))

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'Invalid time "{text}".')

    return hour, minute


def combine_farm_datetime(date_str: str, time_str: str) -> datetime:
    """Combine separate date and time strings in the host's EVENT_TIMEZONE."""
    tz = get_event_timezone()
    year, month, day = parse_farm_date(date_str)
    hour, minute = parse_farm_time(time_str)
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def ensure_timezone_aware(dt: datetime) -> datetime:
    """Attach EVENT_TIMEZONE to naive datetimes loaded from the database."""
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=get_event_timezone())


def parse_start_time(text: str) -> datetime:
    """Legacy single-field parser — splits on the last space into date + time."""
    text = text.strip()
    if not text:
        raise ValueError("Start time cannot be empty.")

    # "6/29/2026 1:00 PM" or "2026-06-29 13:00"
    match = re.match(
        r"^(.+?\d{4})\s+(\d{1,2}:\d{2}(?:\s*[AP]M)?)$",
        text,
        re.I,
    )
    if match:
        return combine_farm_datetime(match.group(1), match.group(2))

    # Time only — use today's date in the event timezone.
    tz = get_event_timezone()
    today = datetime.now(tz).strftime("%m/%d/%Y")
    return combine_farm_datetime(today, text)


def discord_timestamp(dt: datetime, style: str = "F") -> str:
    """Build a Discord dynamic timestamp (renders in each user's timezone)."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def signup_ping_content() -> str | None:
    """Return role ping text for new signup announcements, or None."""
    if config.PING_ROLE_ID is None:
        return None
    return f"<@&{config.PING_ROLE_ID}>"


def signup_allowed_mentions() -> discord.AllowedMentions:
    """Allowed mentions for signup posts so the ping role actually notifies."""
    if config.PING_ROLE_ID is None:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(roles=[discord.Object(id=config.PING_ROLE_ID)])


def format_requirements(requirements: str) -> str:
    """Convert requirement lines into a blockquote list."""
    lines = [line.strip() for line in requirements.strip().splitlines() if line.strip()]
    if not lines:
        return "_No special requirements._"
    return "\n".join(f"> {line.lstrip('•').strip()}" for line in lines)


def _signup_progress_bar(filled: int, total: int) -> str:
    """Render a compact filled/total progress indicator."""
    if total <= 0:
        return "○" * 12
    dots = min(total, 12)
    filled_dots = round(filled / total * dots)
    return f"{'●' * filled_dots}{'○' * (dots - filled_dots)}"


def build_slot_lines(event: FarmEvent) -> str:
    """Render the slot roster with aligned columns."""
    slots = sorted(event.slots, key=lambda s: s.slot_number)
    if not slots:
        return "_No slots configured._"

    width = len(str(slots[-1].slot_number))
    role_width = max(len(slot.role_name) for slot in slots)

    lines: list[str] = []
    for slot in slots:
        num = str(slot.slot_number).rjust(width)
        role = slot.role_name.ljust(role_width)
        if slot.user_id is None:
            occupant = "`Empty`"
        else:
            occupant = f"<@{slot.user_id}>"
        lines.append(f"`{num}` {role}  ›  {occupant}")

    return "\n".join(lines)


def build_farm_embed(event: FarmEvent) -> discord.Embed:
    """Build the signup embed for a farm event."""
    closed = event.closed
    colour = config.EMBED_COLOUR_CLOSED if closed else config.EMBED_COLOUR_OPEN
    filled = event.filled_slots
    total = event.total_slots

    status_line = "🔒 **Signups Closed**" if closed else "🟢 **Signups Open**"
    trip_details = (
        f"{status_line}\n"
        f"{'─' * 28}\n"
        f"📍 **Leaving From**\n{event.leaving_from}\n\n"
        f"🗺️ **Map Type**\n{event.map_type}\n\n"
        f"🕒 **Start Time**\n{discord_timestamp(event.start_time, 'F')}\n"
        f"⏳ **Starts** {discord_timestamp(event.start_time, 'R')}"
    )

    embed = discord.Embed(
        title=event.title,
        description=trip_details,
        colour=colour,
    )

    embed.add_field(
        name="⚠️ Requirements",
        value=format_requirements(event.requirements),
        inline=False,
    )

    progress = _signup_progress_bar(filled, total)
    embed.add_field(
        name=f"👥 Roster  ·  {filled}/{total} filled",
        value=f"{progress}\n\n{build_slot_lines(event)}",
        inline=False,
    )

    footer_parts: list[str] = []
    if closed:
        footer_parts.append("Signups Closed")
    footer_parts.append(f"v{config.BOT_VERSION}")
    footer_parts.append("Sign up in the thread · type a slot number")
    embed.set_footer(text="  ·  ".join(footer_parts))

    return embed


def is_event_host(member: discord.Member) -> bool:
    """Return True if the member may manage events."""
    if member.guild_permissions.administrator:
        return True
    if config.EVENT_HOST_ROLE_ID is None:
        return False
    return any(role.id == config.EVENT_HOST_ROLE_ID for role in member.roles)


def parse_signup_message(content: str) -> tuple[str, int] | None:
    """
    Parse a signup thread message.

    Returns ('claim', slot_number), ('remove', slot_number), or None if ignored.
    """
    text = content.strip()
    if not SIGNUP_MESSAGE_PATTERN.fullmatch(text):
        return None

    slot_number = int(text.lstrip("-"))
    if text.startswith("-"):
        return ("remove", slot_number)
    return ("claim", slot_number)


async def resolve_event_from_interaction(
    db: Database,
    interaction: discord.Interaction,
    event_id: int | None,
) -> FarmEvent | None:
    """Resolve the target event from an optional ID or the current thread."""
    if event_id is not None:
        return await db.get_event_by_id(event_id)

    if isinstance(interaction.channel, discord.Thread):
        return await db.get_event_by_thread(interaction.channel.id)

    return None


async def find_events_for_context(
    db: Database,
    interaction: discord.Interaction,
    event_id: int | None = None,
) -> list[FarmEvent]:
    """Find farm events linked to the interaction channel, thread, or event ID."""
    if event_id is not None:
        event = await db.get_event_by_id(event_id)
        return [event] if event is not None else []

    if isinstance(interaction.channel, discord.Thread):
        event = await db.get_event_by_thread(interaction.channel.id)
        if event is not None:
            return [event]
        if interaction.channel.parent_id is not None:
            return await db.get_events_by_channel(interaction.channel.parent_id)

    if isinstance(interaction.channel, discord.TextChannel):
        return await db.get_events_by_channel(interaction.channel.id)

    return []


async def safe_delete_message(
    message: discord.Message, delay: float = config.MESSAGE_DELETE_DELAY_SECONDS
) -> None:
    """Delete a message after a short delay, ignoring permission errors."""
    try:
        import asyncio

        await asyncio.sleep(delay)
        await message.delete()
    except discord.HTTPException as exc:
        logger.debug("Could not delete message %s: %s", message.id, exc)


async def react_outcome(message: discord.Message, success: bool) -> None:
    """React to a signup message with success or failure."""
    emoji = "✅" if success else "❌"
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException as exc:
        logger.warning("Could not react to message %s: %s", message.id, exc)
