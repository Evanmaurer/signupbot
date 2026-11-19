"""Shared utilities for parsing, permissions, and embed rendering."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

import config
from models import FarmEvent, RoleGroup, SignupStatus

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


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
    """Parse MM/DD (current year), MM/DD/YYYY or YYYY-MM-DD into (year, month, day)."""
    text = text.strip()
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    else:
        us = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2}|\d{4}))?$", text)
        if not us:
            raise ValueError(
                f'Invalid date "{text}". Use **MM/DD** — example: `11/29`'
            )
        month, day = int(us.group(1)), int(us.group(2))
        if us.group(3) is None:
            year = datetime.now(get_event_timezone()).year
        else:
            year = int(us.group(3))
            if year < 100:
                year += 2000

    try:
        datetime(year, month, day)
    except ValueError as exc:
        raise ValueError(f'Invalid date "{text}".') from exc

    return year, month, day


def parse_farm_time(text: str) -> tuple[int, int]:
    """Parse a raid time into (hour, minute).

    Accepts 24-hour (`18`, `18:30`, `1830`), 12-hour (`6 PM`, `6:30pm`),
    with an optional `UTC` / `GMT` / `Z` suffix (times are always EVENT_TIMEZONE).
    """
    original = text
    text = re.sub(r"\s*(utc|gmt|z)$", "", text.strip(), flags=re.I).strip()

    twelve = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M?\.?$", text, re.I)
    twenty_four = re.match(r"^(\d{1,2})(?::?(\d{2}))?$", text)
    if twelve:
        hour, minute = int(twelve.group(1)), int(twelve.group(2) or 0)
        if not 1 <= hour <= 12:
            raise ValueError(f'Invalid time "{original}".')
        if twelve.group(3).upper() == "P" and hour != 12:
            hour += 12
        elif twelve.group(3).upper() == "A" and hour == 12:
            hour = 0
    elif twenty_four:
        hour, minute = int(twenty_four.group(1)), int(twenty_four.group(2) or 0)
    else:
        raise ValueError(
            f'Invalid time "{original}". Use UTC like **18**, **18:30** or **6 PM**.'
        )

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'Invalid time "{original}".')

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


def _progress_bar(filled: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    full = round(filled / total * width)
    return f"{'▰' * full}{'▱' * (width - full)}"


def _role_field_value(group: RoleGroup) -> str:
    lines: list[str] = []
    for slot in group.slots:
        if slot.user_id is None:
            lines.append(f"`{slot.slot_number:>2}` ─")
        else:
            lines.append(f"`{slot.slot_number:>2}` <@{slot.user_id}>")
    value = "\n".join(lines)
    return value[:1024]


def _mention_list(user_ids: list[int]) -> str:
    if not user_ids:
        return ""
    text = ", ".join(f"<@{uid}>" for uid in user_ids)
    return text if len(text) <= 1024 else text[:1000] + f"… (+{len(user_ids)})"


def build_farm_embed(event: FarmEvent, *, with_image: bool = False) -> discord.Embed:
    """Build a Raid-Helper style signup embed for an AVA raid.

    with_image shows the raid comp graphic attached to the post (attachment://raid_comp.png).
    """
    closed = event.closed
    colour = config.EMBED_COLOUR_CLOSED if closed else config.EMBED_COLOUR_OPEN
    filled, total = event.filled_slots, event.total_slots

    status = "🔒 **Signups closed**\n\n" if closed else ""
    description = (
        f"{status}"
        f"📅 {discord_timestamp(event.start_time, 'D')}  ·  "
        f"⏰ {discord_timestamp(event.start_time, 't')}  ·  "
        f"⏳ {discord_timestamp(event.start_time, 'R')}\n\n"
        f"👥 **{filled}/{total}**  {_progress_bar(filled, total)}"
    )

    embed = discord.Embed(title=event.title, description=description, colour=colour)

    for group in event.role_groups()[:20]:
        embed.add_field(
            name=f"{group.emoji} {group.role_name}  ({group.filled}/{group.total})",
            value=_role_field_value(group),
            inline=True,
        )

    tentative = event.users_with_status(SignupStatus.TENTATIVE)
    absence = event.users_with_status(SignupStatus.ABSENCE)
    if tentative:
        embed.add_field(
            name=f"❔ Tentative ({len(tentative)})", value=_mention_list(tentative), inline=False
        )
    if absence:
        embed.add_field(
            name=f"🚫 Absence ({len(absence)})", value=_mention_list(absence), inline=False
        )

    footer = f"Raid ID {event.id}  ·  v{config.BOT_VERSION}"
    if not closed:
        footer += "  ·  Use the dropdown to sign up"
    embed.set_footer(text=footer)
    embed.timestamp = event.start_time
    if with_image:
        embed.set_image(url="attachment://raid_comp.png")
    return embed


def is_event_host(member: discord.Member) -> bool:
    """Return True if the member may manage events."""
    if member.guild_permissions.administrator:
        return True
    if config.EVENT_HOST_ROLE_ID is None:
        return False
    return any(role.id == config.EVENT_HOST_ROLE_ID for role in member.roles)


async def resolve_event_from_interaction(
    db: Database,
    interaction: discord.Interaction,
    event_id: int | None,
) -> FarmEvent | None:
    """Resolve the target event from an ID, or the only raid posted in this channel."""
    if event_id is not None:
        event = await db.get_event_by_id(event_id)
        if event is not None and event.guild_id == interaction.guild_id:
            return event
        return None

    matches = await find_events_for_context(db, interaction)
    if len(matches) == 1:
        return matches[0]
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
