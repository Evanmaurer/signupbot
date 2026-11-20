"""Shared utilities for parsing, permissions, and embed rendering."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
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


# Timezone words people type after a time. Regions (not fixed offsets) so daylight
# saving is handled: "10 pm est" in July still means Eastern time.
TIMEZONE_ALIASES: dict[str, str] = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC", "server": "UTC", "st": "UTC",
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York",
    "eastern": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago",
    "central": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver",
    "mountain": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "akst": "America/Anchorage", "akdt": "America/Anchorage",
    "hst": "Pacific/Honolulu",
    "ast": "America/Halifax", "adt": "America/Halifax",
    "brt": "America/Sao_Paulo",
    "bst": "Europe/London", "uk": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris",
    "eet": "Europe/Athens", "eest": "Europe/Athens",
    "msk": "Europe/Moscow",
    "ist": "Asia/Kolkata",
    "sgt": "Asia/Singapore",
    "jst": "Asia/Tokyo",
    "kst": "Asia/Seoul",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney",
    "awst": "Australia/Perth",
    "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
}
_TZ_SUFFIX = re.compile(r"\s*\b([a-z]{1,8})\.?$", re.I)
_OFFSET_SUFFIX = re.compile(r"\s*\b(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)


def split_timezone(text: str) -> tuple[str, tzinfo | None]:
    """Split a trailing timezone ('est', 'utc+2', ...) off a time string."""
    text = text.strip()
    offset = _OFFSET_SUFFIX.search(text)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        hours, minutes = int(offset.group(2)), int(offset.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ValueError(f'Invalid UTC offset in "{text}".')
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
        return text[: offset.start()].strip(), tz
    word = _TZ_SUFFIX.search(text)
    if word and word.group(1).lower() in TIMEZONE_ALIASES:
        return text[: word.start()].strip(), ZoneInfo(TIMEZONE_ALIASES[word.group(1).lower()])
    return text, None


def parse_farm_time(text: str) -> tuple[int, int]:
    """Parse a raid time (without timezone) into (hour, minute).

    Accepts 24-hour (`18`, `18:30`, `1830`) and 12-hour (`6 PM`, `6:30pm`, `10p`).
    """
    original = text
    text, _ = split_timezone(text)

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
            f'Invalid time "{original}". Use something like **10 PM EST**, '
            "**22:00 CST** or **18 UTC**."
        )

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'Invalid time "{original}".')

    return hour, minute


def server_timezone(name: str | None) -> ZoneInfo:
    """The server's default timezone (from /ava settings), else EVENT_TIMEZONE."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("Unknown server timezone %r — using EVENT_TIMEZONE", name)
    return get_event_timezone()


def combine_farm_datetime(
    date_str: str | None,
    time_str: str,
    *,
    default_date: datetime | None = None,
    default_tz: tzinfo | None = None,
) -> datetime:
    """Combine a date and a time like '10 pm est' into an aware datetime.

    The time's own timezone is used when given, else default_tz (the server's
    timezone), else EVENT_TIMEZONE. With no date, default_date's day (in that
    timezone) is used, or today.
    """
    _, given_tz = split_timezone(time_str)
    tz = given_tz or default_tz or get_event_timezone()
    hour, minute = parse_farm_time(time_str)
    if date_str and date_str.strip():
        year, month, day = parse_farm_date(date_str)
    else:
        base = (default_date or datetime.now(timezone.utc)).astimezone(tz)
        year, month, day = base.year, base.month, base.day
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


def resolve_ping_role_id(setting: int | None) -> int | None:
    """The role to ping for new raids: the server setting, else the config default.

    A setting of 0 means pings are turned off.
    """
    if setting is None:
        return config.PING_ROLE_ID
    return setting or None


def signup_ping_content(role_id: int | None, guild_id: int | None = None) -> str | None:
    """Return role ping text for new signup announcements, or None."""
    if role_id is None:
        return None
    if role_id == guild_id:
        return "@everyone"
    return f"<@&{role_id}>"


def signup_allowed_mentions(
    role_id: int | None, guild_id: int | None = None
) -> discord.AllowedMentions:
    """Allowed mentions for signup posts so the ping role actually notifies."""
    if role_id is None:
        return discord.AllowedMentions.none()
    if role_id == guild_id:
        return discord.AllowedMentions(everyone=True, roles=False, users=False)
    return discord.AllowedMentions(roles=[discord.Object(id=role_id)], users=False)


def _progress_bar(filled: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    full = round(filled / total * width)
    return f"{'▰' * full}{'▱' * (width - full)}"


def _role_field_value(group: RoleGroup) -> str:
    lines: list[str] = []
    for slot in group.slots:
        if slot.user_id is None:
            lines.append(f"`{slot.slot_number}` ─")
        else:
            lines.append(f"`{slot.slot_number}` <@{slot.user_id}>")
    value = "\n".join(lines)
    return value[:1024]


def _mention_list(user_ids: list[int]) -> str:
    if not user_ids:
        return ""
    text = ", ".join(f"<@{uid}>" for uid in user_ids)
    return text if len(text) <= 1024 else text[:1000] + f"… (+{len(user_ids)})"


def build_farm_embed(event: FarmEvent, *, image_name: str | None = None) -> discord.Embed:
    """Build a Raid-Helper style signup embed for an AVA raid.

    image_name is the filename of the picture attached to the post (shown via attachment://).
    """
    closed = event.closed
    if closed:
        colour = config.EMBED_COLOUR_CLOSED
    else:
        colour = event.colour if event.colour is not None else config.EMBED_COLOUR_OPEN
    filled, total = event.filled_slots, event.total_slots

    status = "🔒 **Signups closed**\n\n" if closed else ""
    notes = event.requirements.strip()
    description = (
        f"{status}"
        + (f"{notes}\n\n" if notes else "")
        + f"📅 {discord_timestamp(event.start_time, 'D')}  ·  "
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

    absence = event.users_with_status(SignupStatus.ABSENCE)
    if absence:
        embed.add_field(
            name=f"🚫 Absence ({len(absence)})", value=_mention_list(absence), inline=False
        )

    footer = f"Raid ID {event.id}  ·  v{config.BOT_VERSION}"
    if not closed:
        footer += "  ·  Use the dropdown to sign up"
    embed.set_footer(text=footer)
    embed.timestamp = event.start_time
    if image_name:
        embed.set_image(url=f"attachment://{image_name}")
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
