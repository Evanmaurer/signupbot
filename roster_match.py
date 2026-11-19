"""Match names read from screenshots (OCR) against a raid's signed-up members."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

import discord

from name_match import member_name_keys, normalize_match_name

# Characters OCR commonly confuses in Albion's font, folded to one form.
_OCR_FOLD = str.maketrans({"l": "i", "1": "i", "|": "i", "!": "i", "0": "o"})
_MIN_SIMILARITY = 0.82


def ocr_fold(name: str) -> str:
    text = normalize_match_name(name).translate(_OCR_FOLD)
    return text.replace("rn", "m").replace("vv", "w")


@dataclass(slots=True)
class RosterEntry:
    """One signed-up member and the names they might appear as in-game."""

    user_id: int
    display: str  # best guess at their in-game name
    keys: set[str] = field(default_factory=set)
    folded: set[str] = field(default_factory=set)


def ingame_name(member: discord.Member | discord.User) -> str:
    """Guess the Albion name from a Discord name: '!Name/Alt' -> 'Name'."""
    raw = getattr(member, "nick", None) or member.display_name or member.name
    raw = raw.strip().lstrip("!").strip()
    return raw.split("/", 1)[0].strip() or member.name


def roster_entry(member: discord.Member | discord.User) -> RosterEntry:
    keys = member_name_keys(
        nick=getattr(member, "nick", None),
        display_name=member.display_name,
        name=member.name,
        global_name=member.global_name,
    )
    return RosterEntry(
        user_id=member.id,
        display=ingame_name(member),
        keys=keys,
        folded={ocr_fold(k) for k in keys},
    )


def match_name(
    name: str, roster: list[RosterEntry], taken: set[int]
) -> RosterEntry | None:
    """Find the signed-up member an OCR'd name belongs to (exact, then fuzzy)."""
    candidates = [entry for entry in roster if entry.user_id not in taken]
    if not candidates:
        return None
    norm = normalize_match_name(name)
    for entry in candidates:
        if norm in entry.keys:
            return entry
    folded = ocr_fold(name)
    for entry in candidates:
        if folded in entry.folded:
            return entry

    best: tuple[float, RosterEntry] | None = None
    for entry in candidates:
        for key in entry.folded:
            score = SequenceMatcher(None, folded, key).ratio()
            if best is None or score > best[0]:
                best = (score, entry)
    if best and best[0] >= _MIN_SIMILARITY:
        return best[1]
    return None


def match_names(
    names: list[str], roster: list[RosterEntry], taken: set[int] | None = None
) -> list[tuple[str, int | None]]:
    """Return (name to store, matched user_id or None) for each OCR'd name.

    Matched names are replaced by the member's own spelling, fixing OCR misreads.
    """
    taken = set(taken or ())
    results: list[tuple[str, int | None]] = []
    for name in names:
        entry = match_name(name, roster, taken)
        if entry is None:
            results.append((name, None))
            continue
        taken.add(entry.user_id)
        # An exact (case-insensitive) match keeps the in-game spelling from the
        # screenshot; a fuzzy match uses the member's name to fix the misread.
        exact = normalize_match_name(name) in entry.keys
        results.append((name if exact else entry.display, entry.user_id))
    return results
