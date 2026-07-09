"""Persistent Discord UI views for button-role menus."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models import RolePanel, RolePanelEntry
from role_menu import key_to_partial_emoji, toggle_role

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

BUTTON_CUSTOM_ID_PREFIX = "rolepanel:toggle:"


def button_custom_id(panel_id: int, entry_id: int) -> str:
    return f"{BUTTON_CUSTOM_ID_PREFIX}{panel_id}:{entry_id}"


def parse_button_custom_id(custom_id: str) -> tuple[int, int] | None:
    if not custom_id.startswith(BUTTON_CUSTOM_ID_PREFIX):
        return None
    parts = custom_id.removeprefix(BUTTON_CUSTOM_ID_PREFIX).split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


class RoleToggleButton(discord.ui.Button["RolePanelView"]):
    """Toggle button for a single role mapping."""

    def __init__(self, panel_id: int, entry: RolePanelEntry) -> None:
        emoji = key_to_partial_emoji(entry.emoji_key)
        super().__init__(
            label=entry.label[:80],
            emoji=emoji if isinstance(emoji, (str, discord.PartialEmoji)) else None,
            style=discord.ButtonStyle.secondary,
            custom_id=button_custom_id(panel_id, entry.id),
        )
        self.panel_id = panel_id
        self.entry_id = entry.id
        self.role_id = entry.role_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return

        bot = interaction.client
        db: Database = bot.db  # type: ignore[attr-defined]

        panel = await db.get_role_panel_by_id(self.panel_id)
        if panel is None:
            await interaction.response.send_message(
                "This role menu is no longer active.", ephemeral=True
            )
            return

        entry = panel.entry_by_id(self.entry_id)
        if entry is None:
            await interaction.response.send_message(
                "This button is no longer valid.", ephemeral=True
            )
            return

        role = interaction.guild.get_role(entry.role_id) if interaction.guild else None
        if role is None:
            await interaction.response.send_message(
                "That role no longer exists.", ephemeral=True
            )
            return

        had_role = role in interaction.user.roles
        success = await toggle_role(interaction.user, role)
        if not success:
            await interaction.response.send_message(
                "I couldn't update your role. Ask an admin to check my permissions.",
                ephemeral=True,
            )
            return

        action = "removed" if had_role else "added"
        await interaction.response.send_message(
            f"{'Removed' if had_role else 'Added'} **{role.name}** ({action}).",
            ephemeral=True,
        )


class RolePanelView(discord.ui.View):
    """Persistent view for button-role panels."""

    def __init__(self, panel: RolePanel) -> None:
        super().__init__(timeout=None)
        for entry in sorted(panel.entries, key=lambda e: e.sort_order):
            self.add_item(RoleToggleButton(panel.id, entry))


def build_role_panel_view(panel: RolePanel) -> RolePanelView:
    """Create a persistent button view for a panel."""
    return RolePanelView(panel)
