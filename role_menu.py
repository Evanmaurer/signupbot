"""Shared logic for reaction-role and button-role menus."""

from __future__ import annotations

import logging
import re
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

    lowered = role_text.lower()
    for role in guild.roles:
        if role.name.lower() == lowered:
            return role

    raise ValueError(f'Role "{role_text}" not found. Use an exact role name or @mention.')


def parse_role_mappings(
    text: str, guild: discord.Guild
) -> list[tuple[str, discord.Role, str]]:
    """
    Parse emoji → role mapping lines.

    Returns list of (emoji_key, role, display_label).
    """
    entries: list[tuple[str, discord.Role, str]] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if MAPPING_SPLIT_PATTERN.search(line):
            emoji_part, role_part = MAPPING_SPLIT_PATTERN.split(line, maxsplit=1)
            emoji_part = emoji_part.strip()
            role_part = role_part.strip()
        else:
            match = MAPPING_FALLBACK_PATTERN.match(line)
            if not match:
                raise ValueError(
                    f'Invalid mapping line: "{line}". '
                    'Use format: `🌟 → AVA` or `🌟 AVA`'
                )
            emoji_part, role_part = match.group(1).strip(), match.group(2).strip()

        emoji_key = parse_emoji_token(emoji_part)
        role = resolve_guild_role(guild, role_part)
        display = role_part.lstrip("@")
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


async def assign_role(member: discord.Member, role: discord.Role) -> bool:
    """Add a role if permitted and not already held. Returns success."""
    if role in member.roles:
        return True
    if not can_manage_role(member.guild, role):
        logger.warning(
            "Cannot assign role %s in guild %s — missing permission or hierarchy",
            role.id,
            member.guild.id,
        )
        return False
    try:
        await member.add_roles(role, reason="Role menu assignment")
        return True
    except discord.HTTPException as exc:
        logger.error("Failed to assign role %s to %s: %s", role.id, member.id, exc)
        return False


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
