"""Role icons — Albion item pictures shown next to each role, as bot-owned emojis.

On startup the bot uploads every image in assets/roles/ as an application emoji
(once; they're reused after that) and roles whose name matches get that icon.
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord

logger = logging.getLogger(__name__)

ICON_DIR = Path(__file__).resolve().parent / "assets" / "roles"
EMOJI_PREFIX = "tc_"

# icon file stem -> role names (lower-case) that use it. Checked most specific first.
ROLE_ICON_NAMES: list[tuple[str, tuple[str, ...]]] = [
    ("main_tank", ("main tank", "tank")),
    ("off_tank", ("off tank", "offtank")),
    ("main_heal", ("main healer", "main heal", "healer", "heal")),
    ("sc_support", ("pierce",)),
    ("frost_looter", ("frost looter", "frost", "looter")),
    ("cr_dagger", ("dps/dagger", "dagger dps", "dagger")),
    ("crystal_reaper", ("dps",)),
]

# icon file stem -> "<:tc_name:id>", filled by sync_role_icons().
_EMOJIS: dict[str, str] = {}


def icon_for_role(role_name: str) -> str | None:
    """Return the custom emoji for a role name, if an icon matches."""
    if not _EMOJIS:
        return None
    lowered = role_name.strip().lower()
    # Exact names first, then "contains" (so "Off Tank" never becomes Main Tank).
    for stem, names in ROLE_ICON_NAMES:
        if lowered in names and stem in _EMOJIS:
            return _EMOJIS[stem]
    for stem, names in ROLE_ICON_NAMES:
        if any(name in lowered for name in names) and stem in _EMOJIS:
            return _EMOJIS[stem]
    return None


async def sync_role_icons(client: discord.Client) -> None:
    """Upload missing role icons as application emojis and load their IDs."""
    files = sorted(ICON_DIR.glob("*.png")) if ICON_DIR.exists() else []
    if not files:
        return
    try:
        existing = {e.name: e for e in await client.fetch_application_emojis()}
    except discord.HTTPException as exc:
        logger.warning("Couldn't list application emojis: %s", exc)
        return

    for path in files:
        name = f"{EMOJI_PREFIX}{path.stem}"[:32]
        emoji = existing.get(name)
        if emoji is None:
            try:
                emoji = await client.create_application_emoji(name=name, image=path.read_bytes())
                logger.info("Uploaded role icon %s", name)
            except discord.HTTPException as exc:
                logger.warning("Couldn't upload role icon %s: %s", name, exc)
                continue
        _EMOJIS[path.stem] = str(emoji)

    # Remove old role icons whose image file is gone (e.g. replaced pictures).
    wanted = {f"{EMOJI_PREFIX}{path.stem}"[:32] for path in files}
    for name, emoji in existing.items():
        if name.startswith(EMOJI_PREFIX) and name not in wanted:
            try:
                await emoji.delete()
                logger.info("Removed old role icon %s", name)
            except discord.HTTPException as exc:
                logger.warning("Couldn't remove old role icon %s: %s", name, exc)
    logger.info("Role icons ready: %s", ", ".join(sorted(_EMOJIS)))
