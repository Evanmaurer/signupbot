"""Persistent Raid-Helper style signup components (role dropdown + buttons).

Components use discord.py DynamicItems keyed by raid ID in the custom_id,
so they keep working after a bot restart without re-registering each message.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import discord

from models import FarmEvent

if TYPE_CHECKING:
    from cogs.raid import RaidCog


def _raid_cog(interaction: discord.Interaction) -> RaidCog | None:
    return interaction.client.get_cog("RaidCog")  # type: ignore[union-attr,return-value]


async def _cog_unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Signups are temporarily unavailable — try again in a moment.", ephemeral=True
    )


def _parse_emoji(text: str) -> discord.PartialEmoji | str | None:
    try:
        return discord.PartialEmoji.from_str(text)
    except Exception:
        return None


class RoleSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"raid:role:(?P<event_id>\d+)",
):
    """Dropdown listing each role with open/filled counts."""

    def __init__(self, event_id: int, options: list[discord.SelectOption] | None = None,
                 *, disabled: bool = False, placeholder: str = "🎯 Pick your role…") -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=f"raid:role:{event_id}",
                placeholder=placeholder,
                min_values=1,
                max_values=1,
                options=options or [discord.SelectOption(label="…", value="_")],
                disabled=disabled,
                row=0,
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match[str], /
    ) -> RoleSelect:
        return cls(int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> Any:
        cog = _raid_cog(interaction)
        if cog is None:
            return await _cog_unavailable(interaction)
        await cog.handle_role_pick(interaction, self.event_id, self.item.values[0])


class RaidActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"raid:(?P<action>tentative|absence|leave):(?P<event_id>\d+)",
):
    """Tentative / Absence / Leave buttons."""

    STYLES = {
        "tentative": ("Tentative", "❔", discord.ButtonStyle.secondary),
        "absence": ("Absence", "🚫", discord.ButtonStyle.secondary),
        "leave": ("Leave", "🚪", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, event_id: int, *, disabled: bool = False) -> None:
        label, emoji, style = self.STYLES[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"raid:{action}:{event_id}",
                disabled=disabled,
                row=1,
            )
        )
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> RaidActionButton:
        return cls(match["action"], int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> Any:
        cog = _raid_cog(interaction)
        if cog is None:
            return await _cog_unavailable(interaction)
        await cog.handle_action(interaction, self.event_id, self.action)


def build_role_options(event: FarmEvent) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for group in event.role_groups()[:25]:
        open_count = group.total - group.filled
        description = "FULL" if open_count <= 0 else f"{open_count} open · {group.filled}/{group.total} filled"
        options.append(
            discord.SelectOption(
                label=group.role_name[:100],
                value=group.role_name[:100],
                description=description,
                emoji=_parse_emoji(group.emoji),
            )
        )
    return options


def build_raid_view(event: FarmEvent) -> discord.ui.View:
    """Build the signup controls shown under a raid embed."""
    view = discord.ui.View(timeout=None)
    closed = event.closed
    view.add_item(
        RoleSelect(
            event.id,
            build_role_options(event),
            disabled=closed,
            placeholder="🔒 Signups are closed" if closed else "🎯 Pick your role…",
        )
    )
    for action in ("tentative", "absence", "leave"):
        view.add_item(RaidActionButton(action, event.id, disabled=closed))
    return view


DYNAMIC_ITEMS = (RoleSelect, RaidActionButton)
