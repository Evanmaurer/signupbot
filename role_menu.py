"""Shared logic for reaction-role and button-role menus."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from models import RolePanel, RolePanelEntry, RolePanelType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

CUSTOM_EMOJI_PATTERN = re.compile(r"^<a?:(\w+):(\d+)>$")
ROLE_MENTION_PATTERN = re.compile(r"^<@&(\d+)>$")
MAPPING_SPLIT_PATTERN = re.compile(r"\s*(?:→|—|->|-\>)\s*")
MAPPING_FALLBACK_PATTERN = re.compile(r"^(<a?:\w+:\d+>|\S+)\s+(.+)$")
MAX_PANEL_ENTRIES = 20


def emoji_to_key(emoji: discord.PartialEmoji | str) -> str:
    """Normalize an emoji to a stable storage key."""
    if isinstance(emoji, str):
        custom = CUSTOM_EMOJI_PATTERN.match(emoji.strip())
        if custom:
            return f"{custom.group(1)}:{custom.group(2)}"
        return emoji
    if emoji.id:
        return f"{emoji.name}:{emoji.id}"
    return str(emoji)


def key_to_partial_emoji(key: str) -> str | discord.PartialEmoji:
    """Convert a storage key back to something discord.py accepts."""
    if ":" in key:
        name, _, emoji_id = key.partition(":")
        if emoji_id.isdigit():
            return discord.PartialEmoji(name=name, id=int(emoji_id))
    return key


def parse_emoji_token(token: str) -> str:
    """Parse and validate an emoji token from mapping input."""
    token = token.strip()
    if CUSTOM_EMOJI_PATTERN.match(token):
        return emoji_to_key(token)
    if not token:
        raise ValueError("Emoji cannot be empty.")
    return token


def resolve_guild_role(guild: discord.Guild, role_text: str) -> discord.Role:
    """Resolve a role from mention, ID, or name."""
    role_text = role_text.strip()
    mention = ROLE_MENTION_PATTERN.match(role_text)
    if mention:
        role = guild.get_role(int(mention.group(1)))
        if role is None:
            raise ValueError(f"Role mention {role_text} not found in this server.")
        return role

    if role_text.isdigit():
        role = guild.get_role(int(role_text))
        if role is None:
            raise ValueError(f"Role ID {role_text} not found in this server.")
        return role

    # @Role Name typed in slash command (not a real mention)
    if role_text.startswith("@"):
        role_text = role_text[1:].strip()

    lowered = role_text.lower()
    for role in guild.roles:
        if role.name.lower() == lowered:
            return role

    raise ValueError(f'Role "{role_text}" not found. Use an exact role name or @mention.')


def _split_role_and_next_emoji(role_segment: str) -> tuple[str, str | None]:
    """When multiple mappings are on one line, peel the next emoji off the role segment."""
    # Pairs are separated by two or more spaces before the next emoji.
    match = re.search(r"\s{2,}(.+)$", role_segment.strip())
    if match:
        role_part = role_segment[: match.start()].strip()
        next_emoji = match.group(1).strip()
        return role_part, next_emoji
    return role_segment.strip(), None


def _parse_mapping_line(line: str) -> list[tuple[str, str]]:
    """Parse one line into (emoji, role) pairs (supports multiple pairs per line)."""
    line = line.strip()
    if not line:
        return []

    if not MAPPING_SPLIT_PATTERN.search(line):
        match = MAPPING_FALLBACK_PATTERN.match(line)
        if not match:
            raise ValueError(
                f'Invalid mapping line: "{line}". '
                "Use format: `🌟 → AVA` or one mapping per line."
            )
        return [(match.group(1).strip(), match.group(2).strip())]

    segments = MAPPING_SPLIT_PATTERN.split(line)
    if len(segments) < 2:
        raise ValueError(f'Invalid mapping line: "{line}".')

    pairs: list[tuple[str, str]] = []
    emoji_part = segments[0].strip()

    for index in range(1, len(segments)):
        role_segment = segments[index]
        if index < len(segments) - 1:
            role_part, next_emoji = _split_role_and_next_emoji(role_segment)
            pairs.append((emoji_part, role_part))
            if next_emoji is None:
                raise ValueError(
                    f'Could not parse mapping in: "{line}". '
                    "Put each mapping on its own line."
                )
            emoji_part = next_emoji
        else:
            pairs.append((emoji_part, role_segment.strip()))

    return pairs


def parse_role_mappings(
    text: str, guild: discord.Guild
) -> list[tuple[str, discord.Role, str]]:
    """
    Parse emoji → role mapping lines.

    Returns list of (emoji_key, role, display_label).
    """
    entries: list[tuple[str, discord.Role, str]] = []

    # Allow one mapping per line, or several on a single line separated by arrows.
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        raise ValueError("Provide at least one emoji → role mapping.")

    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        for emoji_part, role_part in _parse_mapping_line(line):
            emoji_key = parse_emoji_token(emoji_part)
            role = resolve_guild_role(guild, role_part)
            display = role.name
            entries.append((emoji_key, role, display))

    if not entries:
        raise ValueError("Provide at least one emoji → role mapping.")
    if len(entries) > MAX_PANEL_ENTRIES:
        raise ValueError(f"Maximum {MAX_PANEL_ENTRIES} role mappings allowed.")

    emoji_keys = [key for key, _, _ in entries]
    if len(emoji_keys) != len(set(emoji_keys)):
        raise ValueError("Duplicate emoji mappings are not allowed.")

    role_ids = [role.id for _, role, _ in entries]
    if len(role_ids) != len(set(role_ids)):
        raise ValueError("Each mapping must point to a unique role.")

    return entries


def build_role_panel_embed(panel: RolePanel) -> discord.Embed:
    """Build the role selection embed."""
    mapping_lines = []
    for entry in sorted(panel.entries, key=lambda e: e.sort_order):
        emoji_display = key_to_display(entry.emoji_key)
        mapping_lines.append(f"{emoji_display} — **{entry.label}**")

    body = panel.description.strip()
    if mapping_lines:
        body = f"{body}\n\n" + "\n".join(mapping_lines) if body else "\n".join(mapping_lines)

    embed = discord.Embed(
        title=panel.title,
        description=body,
        colour=0x5865F2,
    )
    panel_type_label = "Buttons" if panel.panel_type == RolePanelType.BUTTON else "Reactions"
    embed.set_footer(text=f"Role menu · {panel_type_label}")
    return embed


def key_to_display(key: str) -> str:
    """Format emoji key for embed display."""
    partial = key_to_partial_emoji(key)
    if isinstance(partial, discord.PartialEmoji):
        return str(partial)
    return key


def can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    """Return True if the bot can assign the given role."""
    if guild.me is None:
        return False
    if not guild.me.guild_permissions.manage_roles:
        return False
    return guild.me.top_role > role


class RoleAssignStatus:
    """Outcome categories for bulk role assignment."""

    ASSIGNED = "assigned"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class RoleAssignOutcome:
    """Result of attempting to assign a role to one member."""

    display_name: str
    user_id: int
    status: str
    reason: str


def validate_role_assignable(guild: discord.Guild, role: discord.Role) -> None:
    """Raise ValueError if the bot cannot assign this role."""
    if role.is_default():
        raise ValueError("You cannot assign @everyone.")
    if role.managed:
        raise ValueError(
            f"Role **{role.name}** is managed by an integration and cannot be assigned."
        )
    if guild.me is None:
        raise ValueError("Bot member is not available in this server.")
    if not guild.me.guild_permissions.manage_roles:
        raise ValueError("I need the **Manage Roles** permission.")
    if role >= guild.me.top_role:
        raise ValueError(
            f"My highest role must be **above** **{role.name}** to assign it."
        )


async def assign_role(
    member: discord.Member,
    role: discord.Role,
    *,
    reason: str = "Role menu assignment",
) -> bool:
    """Add a role if permitted and not already held. Returns success."""
    outcome = await assign_role_detailed(member, role, reason=reason)
    return outcome.status != RoleAssignStatus.FAILED


async def assign_role_detailed(
    member: discord.Member,
    role: discord.Role,
    *,
    reason: str = "Role assignment",
) -> RoleAssignOutcome:
    """Assign a role and return a detailed outcome."""
    if role in member.roles:
        return RoleAssignOutcome(
            display_name=member.display_name,
            user_id=member.id,
            status=RoleAssignStatus.SKIPPED,
            reason="already has role",
        )

    if not can_manage_role(member.guild, role):
        logger.warning(
            "Cannot assign role %s in guild %s — missing permission or hierarchy",
            role.id,
            member.guild.id,
        )
        return RoleAssignOutcome(
            display_name=member.display_name,
            user_id=member.id,
            status=RoleAssignStatus.SKIPPED,
            reason="missing permissions",
        )

    try:
        await member.add_roles(role, reason=reason)
        return RoleAssignOutcome(
            display_name=member.display_name,
            user_id=member.id,
            status=RoleAssignStatus.ASSIGNED,
            reason="assigned",
        )
    except discord.Forbidden:
        logger.error(
            "Forbidden assigning role %s to %s", role.id, member.id
        )
        return RoleAssignOutcome(
            display_name=member.display_name,
            user_id=member.id,
            status=RoleAssignStatus.FAILED,
            reason="missing permissions",
        )
    except discord.HTTPException as exc:
        logger.error("Failed to assign role %s to %s: %s", role.id, member.id, exc)
        return RoleAssignOutcome(
            display_name=member.display_name,
            user_id=member.id,
            status=RoleAssignStatus.FAILED,
            reason="unable to assign role",
        )


async def assign_role_to_members(
    members: list[discord.Member],
    role: discord.Role,
    *,
    reason: str,
    delay_seconds: float = 0.35,
) -> list[RoleAssignOutcome]:
    """Assign a role to many members, continuing on individual failures."""
    import asyncio

    outcomes: list[RoleAssignOutcome] = []
    seen: set[int] = set()

    for member in members:
        if member.id in seen:
            continue
        seen.add(member.id)
        outcomes.append(await assign_role_detailed(member, role, reason=reason))
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    return outcomes


def build_bulk_role_embed(
    role: discord.Role,
    outcomes: list[RoleAssignOutcome],
    *,
    moderator: discord.Member | discord.User,
) -> discord.Embed:
    """Build a summary embed for bulk role assignment."""
    assigned = [o for o in outcomes if o.status == RoleAssignStatus.ASSIGNED]
    skipped = [o for o in outcomes if o.status == RoleAssignStatus.SKIPPED]
    failed = [o for o in outcomes if o.status == RoleAssignStatus.FAILED]

    colour = 0x57F287 if assigned and not failed else (0xFEE75C if skipped or assigned else 0xED4245)
    embed = discord.Embed(
        title="Role Assignment",
        colour=colour,
    )

    if assigned:
        names = ", ".join(o.display_name for o in assigned[:25])
        if len(assigned) > 25:
            names += f" … +{len(assigned) - 25} more"
        embed.add_field(
            name=f"✅ Role \"{role.name}\" assigned to {len(assigned)} user(s)",
            value=names or "—",
            inline=False,
        )
    else:
        embed.add_field(
            name=f"✅ Role \"{role.name}\" assigned",
            value="No new assignments.",
            inline=False,
        )

    if skipped:
        lines = [f"• {o.display_name} ({o.reason})" for o in skipped[:20]]
        if len(skipped) > 20:
            lines.append(f"… +{len(skipped) - 20} more")
        embed.add_field(name="⚠️ Skipped", value="\n".join(lines), inline=False)

    if failed:
        lines = [f"• {o.display_name} ({o.reason})" for o in failed[:20]]
        if len(failed) > 20:
            lines.append(f"… +{len(failed) - 20} more")
        embed.add_field(name="❌ Failed", value="\n".join(lines), inline=False)

    embed.set_footer(
        text=(
            f"By {moderator} · Total processed: {len(outcomes)} · "
            f"{len(assigned)} assigned · {len(skipped)} skipped · {len(failed)} failed"
        )
    )
    return embed


USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")


def parse_user_mentions(text: str) -> list[int]:
    """Extract unique user IDs from a mention string."""
    ids: list[int] = []
    seen: set[int] = set()
    for match in USER_MENTION_PATTERN.finditer(text):
        user_id = int(match.group(1))
        if user_id not in seen:
            seen.add(user_id)
            ids.append(user_id)
    return ids


async def resolve_members_from_ids(
    guild: discord.Guild, user_ids: list[int]
) -> tuple[list[discord.Member], list[int]]:
    """Resolve member objects; return (found_members, missing_ids)."""
    members: list[discord.Member] = []
    missing: list[int] = []
    for user_id in user_ids:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                missing.append(user_id)
                continue
            except discord.HTTPException:
                missing.append(user_id)
                continue
        members.append(member)
    return members, missing


async def remove_role(member: discord.Member, role: discord.Role) -> bool:
    """Remove a role if permitted and currently held. Returns success."""
    if role not in member.roles:
        return True
    if not can_manage_role(member.guild, role):
        logger.warning(
            "Cannot remove role %s in guild %s — missing permission or hierarchy",
            role.id,
            member.guild.id,
        )
        return False
    try:
        await member.remove_roles(role, reason="Role menu removal")
        return True
    except discord.HTTPException as exc:
        logger.error("Failed to remove role %s from %s: %s", role.id, member.id, exc)
        return False


async def toggle_role(member: discord.Member, role: discord.Role) -> bool:
    """Toggle a role on or off for a member."""
    if role in member.roles:
        return await remove_role(member, role)
    return await assign_role(member, role)
