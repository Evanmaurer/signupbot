"""Shared name normalization for Discord ↔ in-game matching."""

from __future__ import annotations

from typing import Iterable


def normalize_match_name(name: str | None) -> str:
    """
    Normalize a player/Discord name for matching.

    Strips whitespace, lowercases, and drops a leading ``!`` used by some
    Discord nicknames before the in-game name (e.g. ``!Player`` → ``player``).
    """
    text = (name or "").strip().casefold()
    if text.startswith("!"):
        text = text[1:].lstrip()
    return text


def name_match_keys(name: str | None) -> set[str]:
    """
    Keys used to index or look up a name.

    Includes the normalized full name and, if present, the segment before ``/``
    (e.g. ``ObscureOtter/ObeseOtter`` also matches ``ObscureOtter``).
    """
    normalized = normalize_match_name(name)
    if not normalized:
        return set()

    keys = {normalized}
    if "/" in normalized:
        prefix = normalized.split("/", 1)[0].strip()
        if prefix:
            keys.add(prefix)
    return keys


def member_name_keys(
    *,
    nick: str | None = None,
    display_name: str | None = None,
    name: str | None = None,
    global_name: str | None = None,
) -> set[str]:
    """Build all match keys from a Discord member's name fields."""
    keys: set[str] = set()
    for value in (nick, display_name, name, global_name):
        keys.update(name_match_keys(value))
    return keys


def expand_name_keys(names: Iterable[str | None]) -> set[str]:
    """Union of match keys for many raw names."""
    keys: set[str] = set()
    for value in names:
        keys.update(name_match_keys(value))
    return keys
