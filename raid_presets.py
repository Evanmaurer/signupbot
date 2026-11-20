"""Preset role parsing and Albion AVA role emoji helpers."""

from __future__ import annotations

import re

# Default AVA fame-farm roster, written in preset format.
DEFAULT_PRESET_NAME = "AVA Fame Farm"
DEFAULT_PRESET_TITLE = "⚔️ AVA Fame Farm"
DEFAULT_PRESET_ROLES = """🛡️ Main Tank
🛡️ Off Tank
💚 Main Healer
🏹 Pierce
❄️ Frost Looter
⚔️ DPS x3
🗡️ DPS/Dagger x2"""

MAX_ROLE_GROUPS = 20  # Select menus allow 25 options; embeds allow 25 fields.
MAX_SLOTS_PER_ROLE = 20
MAX_TOTAL_SLOTS = 60

# Keyword → emoji used when a preset role line has no emoji of its own.
_ROLE_EMOJI_KEYWORDS: list[tuple[str, str]] = [
    ("off tank", "🛡️"),
    ("tank", "🛡️"),
    ("heal", "💚"),
    ("cobra", "🐍"),
    ("root", "🌳"),
    ("scout", "🔭"),
    ("support", "🔮"),
    ("arcane", "🔮"),
    ("bomb", "💥"),
    ("mount", "🐘"),
    ("battlemount", "🐘"),
    ("dps", "⚔️"),
    ("dmg", "⚔️"),
    ("damage", "⚔️"),
    ("looter", "💰"),
    ("bench", "🪑"),
]
DEFAULT_ROLE_EMOJI = "🔹"

_CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
_COUNT_SUFFIX = re.compile(r"^(.*?)\s*[x×*]\s*(\d{1,2})$", re.I)
_COUNT_PREFIX = re.compile(r"^(\d{1,2})\s*[x×*]\s*(.+)$", re.I)


# Emojis the bot picks automatically; roles using one of these get the built-in
# Albion icon instead when there is one for that role name.
_GENERIC_EMOJIS = {e.replace("️", "") for _, e in _ROLE_EMOJI_KEYWORDS} | {
    DEFAULT_ROLE_EMOJI, "🏹", "❄️".replace("️", ""), "🗡️".replace("️", ""),
}


def is_generic_emoji(emoji: str) -> bool:
    return emoji.replace("️", "") in _GENERIC_EMOJIS


def default_role_emoji(role_name: str) -> str:
    """Pick a sensible emoji for an Albion role name."""
    lowered = role_name.lower()
    for keyword, emoji in _ROLE_EMOJI_KEYWORDS:
        if keyword in lowered:
            return emoji
    return DEFAULT_ROLE_EMOJI


def _split_emoji(text: str) -> tuple[str | None, str]:
    """Split a leading emoji (unicode or <:custom:id>) off a role line."""
    first, _, rest = text.partition(" ")
    if not rest:
        return None, text
    if _CUSTOM_EMOJI.match(first):
        return first, rest.strip()
    if not any(ch.isalnum() for ch in first):
        return first, rest.strip()
    return None, text


def parse_roles(text: str) -> list[tuple[str, str, int]]:
    """
    Parse preset role lines into (emoji, role_name, count).

    Accepted line formats (one role per line):
        Tank
        🛡️ Tank
        DPS x5
        ⚔️ DPS x5
        5x DPS
    """
    groups: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    total = 0

    for raw_line in text.strip().splitlines():
        line = raw_line.strip().lstrip("-•").strip()
        if not line:
            continue

        emoji, body = _split_emoji(line)
        count = 1
        match = _COUNT_SUFFIX.match(body)
        if match and match.group(1).strip():
            body, count = match.group(1).strip(), int(match.group(2))
        else:
            match = _COUNT_PREFIX.match(body)
            if match:
                count, body = int(match.group(1)), match.group(2).strip()

        name = body.strip()
        if not name:
            raise ValueError(f'Invalid role line: "{raw_line.strip()}".')
        if len(name) > 50:
            raise ValueError(f'Role name too long (max 50): "{name}".')
        if not 1 <= count <= MAX_SLOTS_PER_ROLE:
            raise ValueError(
                f'"{name}" must have between 1 and {MAX_SLOTS_PER_ROLE} slots.'
            )

        key = name.lower()
        if key in seen:
            raise ValueError(
                f'Role "{name}" is listed twice — use a count instead, e.g. `{name} x2`.'
            )
        seen.add(key)

        groups.append((emoji or default_role_emoji(name), name, count))
        total += count

    if not groups:
        raise ValueError("A preset needs at least one role.")
    if len(groups) > MAX_ROLE_GROUPS:
        raise ValueError(f"A preset can have at most {MAX_ROLE_GROUPS} different roles.")
    if total > MAX_TOTAL_SLOTS:
        raise ValueError(f"A preset can have at most {MAX_TOTAL_SLOTS} total slots.")

    return groups


def expand_roles(groups: list[tuple[str, str, int]]) -> list[tuple[int, str, str]]:
    """Turn role groups into numbered slots: (slot_number, role_name, emoji)."""
    slots: list[tuple[int, str, str]] = []
    number = 1
    for emoji, name, count in groups:
        for _ in range(count):
            slots.append((number, name, emoji))
            number += 1
    return slots


def format_roles(groups: list[tuple[str, str, int]]) -> str:
    """Render role groups back into editable preset text."""
    return "\n".join(
        f"{emoji} {name}" + (f" x{count}" if count > 1 else "")
        for emoji, name, count in groups
    )


def roles_summary(groups: list[tuple[str, str, int]]) -> str:
    """One-line summary like '🛡️ Tank · ⚔️ DPS ×5'."""
    return " · ".join(
        f"{emoji} {name}" + (f" ×{count}" if count > 1 else "")
        for emoji, name, count in groups
    )
