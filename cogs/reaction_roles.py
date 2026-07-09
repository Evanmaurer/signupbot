"""Reaction-role and button-role menu commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models import RolePanel, RolePanelEntry, RolePanelType
from role_menu import (
    assign_role,
    build_role_panel_embed,
    emoji_to_key,
    key_to_partial_emoji,
    parse_emoji_token,
    parse_role_mappings,
    remove_role,
)
from utils import is_event_host
from views.reaction_role_view import build_role_panel_view

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


class RolePanelSelect(discord.ui.Select):
    """Select menu for choosing a role panel."""

    def __init__(self, panels: list[tuple[int, str]]) -> None:
        options = [
            discord.SelectOption(label=label[:100], value=str(panel_id))
            for panel_id, label in panels[:25]
        ]
        super().__init__(
            placeholder="Select a role menu…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.selected_panel_id: int | None = None

    async def callback(self, interaction: discord.Interaction) -> None:
        self.selected_panel_id = int(self.values[0])
        self.view.stop()  # type: ignore[attr-defined]
        await interaction.response.defer()


class RolePanelPickerView(discord.ui.View):
    def __init__(self, panels: list[tuple[int, str]]) -> None:
        super().__init__(timeout=60)
        self.select = RolePanelSelect(panels)
        self.add_item(self.select)

    @property
    def panel_id(self) -> int | None:
        return self.select.selected_panel_id


class ReactionRolesCog(commands.Cog):
    """Reaction-role and button-role menus."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def cog_load(self) -> None:
        panels = await self.db.get_button_role_panels()
        for panel in panels:
            self.bot.add_view(build_role_panel_view(panel))
        logger.info("Restored %d button role panel(s)", len(panels))

    async def _require_host(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return False
        if not is_event_host(interaction.user):
            await interaction.response.send_message(
                "You need the Event Host role or Administrator permission.",
                ephemeral=True,
            )
            return False
        return True

    async def _resolve_panel_or_reply(
        self,
        interaction: discord.Interaction,
        panel_id: int | None,
        *,
        message_id: int | None = None,
    ) -> RolePanel | None:
        if panel_id is not None:
            panel = await self.db.get_role_panel_by_id(panel_id)
            if panel is None:
                await interaction.response.send_message(
                    f"No role menu found with ID {panel_id}.", ephemeral=True
                )
            return panel

        if message_id is not None:
            panel = await self.db.get_role_panel_by_message(message_id)
            if panel is None:
                await interaction.response.send_message(
                    "No role menu linked to that message.", ephemeral=True
                )
            return panel

        if isinstance(interaction.channel, discord.TextChannel):
            panels = await self.db.get_role_panels_by_channel(interaction.channel.id)
            if len(panels) == 1:
                return panels[0]

        guild_panels = await self.db.get_role_panels_by_guild(
            interaction.guild_id  # type: ignore[arg-type]
        )
        if not guild_panels:
            await interaction.response.send_message(
                "No role menus found in this server.", ephemeral=True
            )
            return None

        if len(guild_panels) == 1:
            return guild_panels[0]

        picker_items = [
            (p.id, f"{p.title[:60]} (#{p.id})") for p in guild_panels
        ]
        view = RolePanelPickerView(picker_items)
        await interaction.response.send_message(
            "Multiple role menus found. Select one:",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.panel_id is None:
            return None
        return await self.db.get_role_panel_by_id(view.panel_id)

    async def _refresh_panel_message(self, panel: RolePanel) -> None:
        channel = self.bot.get_channel(panel.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(panel.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(panel.message_id)
        except discord.HTTPException as exc:
            logger.warning("Could not fetch role panel message %s: %s", panel.message_id, exc)
            return

        embed = build_role_panel_embed(panel)
        if panel.panel_type == RolePanelType.BUTTON:
            await message.edit(embed=embed, view=build_role_panel_view(panel))
        else:
            await message.edit(embed=embed)

    async def _create_panel(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        role_mappings: str,
        panel_type: str,
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                "Use this command in a text channel.", ephemeral=True
            )
            return

        if interaction.guild is None:
            return

        try:
            mappings = parse_role_mappings(role_mappings, interaction.guild)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        panel_data = [(key, role.id, label) for key, role, label in mappings]
        embed_panel = RolePanel(
            id=0,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            message_id=0,
            creator_id=interaction.user.id,
            title=title,
            description=description,
            panel_type=panel_type,
            created_at="",
            entries=[],
        )
        embed_panel.entries = [
            RolePanelEntry(
                id=i,
                panel_id=0,
                emoji_key=key,
                role_id=role.id,
                label=label,
                sort_order=i,
            )
            for i, (key, role, label) in enumerate(mappings)
        ]

        embed = build_role_panel_embed(embed_panel)

        try:
            message = await interaction.channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            logger.error("Failed to post role menu: %s", exc)
            await interaction.followup.send(f"Failed to create message: {exc}", ephemeral=True)
            return

        if panel_type == RolePanelType.REACTION:
            for emoji_key, _, _ in mappings:
                emoji = key_to_partial_emoji(emoji_key)
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException as exc:
                    logger.warning("Could not add reaction %s: %s", emoji_key, exc)

        panel = await self.db.create_role_panel(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            message_id=message.id,
            creator_id=interaction.user.id,
            title=title,
            description=description,
            panel_type=panel_type,
            entries=panel_data,
        )

        if panel_type == RolePanelType.BUTTON:
            view = build_role_panel_view(panel)
            self.bot.add_view(view)
            await message.edit(view=view)

        kind = "Button" if panel_type == RolePanelType.BUTTON else "Reaction"
        await interaction.followup.send(
            f"{kind} role menu created: {message.jump_url}",
            ephemeral=True,
        )
        logger.info(
            "Role panel %s created (%s) in guild %s",
            panel.id,
            panel_type,
            interaction.guild.id,
        )

    @app_commands.command(
        name="reactionroles",
        description="Create a reaction-role selection message.",
    )
    @app_commands.describe(
        title="Embed title",
        description="Embed description shown above the role list",
        role_mappings="One mapping per line: 🌟 → AVA or 🌟 AVA (up to 20)",
    )
    async def reactionroles(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        role_mappings: str,
    ) -> None:
        if not await self._require_host(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._create_panel(
            interaction,
            title=title,
            description=description,
            role_mappings=role_mappings,
            panel_type=RolePanelType.REACTION,
        )

    @app_commands.command(
        name="buttonroles",
        description="Create a button-role selection message (recommended for mobile).",
    )
    @app_commands.describe(
        title="Embed title",
        description="Embed description shown above the role list",
        role_mappings="One mapping per line: 🌟 → AVA or 🌟 AVA (up to 20)",
    )
    async def buttonroles(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        role_mappings: str,
    ) -> None:
        if not await self._require_host(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._create_panel(
            interaction,
            title=title,
            description=description,
            role_mappings=role_mappings,
            panel_type=RolePanelType.BUTTON,
        )

    @app_commands.command(
        name="editreactionroles",
        description="Edit a reaction-role or button-role menu.",
    )
    @app_commands.describe(
        panel_id="Panel ID (optional — pick from list if omitted)",
        title="New title",
        description="New description",
        add_mappings="New mappings to add (one per line)",
        remove_emojis="Emojis to remove (one per line)",
    )
    async def editreactionroles(
        self,
        interaction: discord.Interaction,
        panel_id: int | None = None,
        title: str | None = None,
        description: str | None = None,
        add_mappings: str | None = None,
        remove_emojis: str | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        if not any([title, description, add_mappings, remove_emojis]):
            await interaction.response.send_message(
                "Provide at least one field to update.", ephemeral=True
            )
            return

        panel = await self._resolve_panel_or_reply(interaction, panel_id)
        if panel is None:
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            return

        if add_mappings:
            try:
                new_entries = parse_role_mappings(add_mappings, interaction.guild)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            existing_emojis = {e.emoji_key for e in panel.entries}
            existing_roles = {e.role_id for e in panel.entries}
            filtered: list[tuple[str, int, str]] = []
            for key, role, label in new_entries:
                if key in existing_emojis:
                    await interaction.followup.send(
                        f"Emoji `{key}` is already on this menu.", ephemeral=True
                    )
                    return
                if role.id in existing_roles:
                    await interaction.followup.send(
                        f"Role **{role.name}** is already on this menu.", ephemeral=True
                    )
                    return
                filtered.append((key, role.id, label))

            total = len(panel.entries) + len(filtered)
            if total > 20:
                await interaction.followup.send(
                    "A menu can have at most 20 role mappings.", ephemeral=True
                )
                return

            await self.db.add_role_panel_entries(panel.id, filtered)

            if panel.panel_type == RolePanelType.REACTION:
                channel = await self._fetch_text_channel(panel.channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(panel.message_id)
                        for key, _, _ in filtered:
                            await message.add_reaction(key_to_partial_emoji(key))
                    except discord.HTTPException as exc:
                        logger.warning("Could not add reactions during edit: %s", exc)

        if remove_emojis:
            keys_to_remove: list[str] = []
            for line in remove_emojis.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                keys_to_remove.append(parse_emoji_token(line))
            removed = await self.db.remove_role_panel_entries_by_emoji(
                panel.id, keys_to_remove
            )
            if removed == 0:
                await interaction.followup.send(
                    "No matching emoji mappings were removed.", ephemeral=True
                )
                return

        if title is not None or description is not None:
            await self.db.update_role_panel(
                panel.id, title=title, description=description
            )

        refreshed = await self.db.get_role_panel_by_id(panel.id)
        if refreshed:
            await self._refresh_panel_message(refreshed)

        await interaction.followup.send(
            f"Updated role menu **{refreshed.title if refreshed else panel.title}**.",
            ephemeral=True,
        )

    @app_commands.command(
        name="deletereactionroles",
        description="Delete a reaction-role or button-role menu.",
    )
    @app_commands.describe(panel_id="Panel ID (optional)")
    async def deletereactionroles(
        self,
        interaction: discord.Interaction,
        panel_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        panel = await self._resolve_panel_or_reply(interaction, panel_id)
        if panel is None:
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        channel = await self._fetch_text_channel(panel.channel_id)
        if channel:
            try:
                message = await channel.fetch_message(panel.message_id)
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                logger.warning("Could not delete role panel message: %s", exc)

        await self.db.delete_role_panel(panel.id)
        await interaction.followup.send(
            f"Deleted role menu **{panel.title}**.", ephemeral=True
        )

    @app_commands.command(
        name="listreactionroles",
        description="List all active role menus in this server.",
    )
    async def listreactionroles(self, interaction: discord.Interaction) -> None:
        if not await self._require_host(interaction):
            return

        panels = await self.db.get_role_panels_by_guild(
            interaction.guild_id  # type: ignore[arg-type]
        )
        if not panels:
            await interaction.response.send_message(
                "No role menus in this server.", ephemeral=True
            )
            return

        lines: list[str] = []
        for panel in panels:
            kind = "Buttons" if panel.panel_type == RolePanelType.BUTTON else "Reactions"
            lines.append(
                f"**#{panel.id}** · {panel.title}\n"
                f"<#{panel.channel_id}> · {kind} · {len(panel.entries)} roles"
            )

        embed = discord.Embed(
            title="🎭 Role Menus",
            description="\n\n".join(lines),
            colour=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _fetch_text_channel(self, channel_id: int) -> discord.TextChannel | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _handle_raw_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        adding: bool,
    ) -> None:
        if payload.user_id == self.bot.user.id:
            return

        panel = await self.db.get_role_panel_by_message(payload.message_id)
        if panel is None or panel.panel_type != RolePanelType.REACTION:
            return

        emoji_key = emoji_to_key(payload.emoji)
        entry = panel.entry_by_emoji(emoji_key)
        if entry is None:
            return

        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return

        if member.bot:
            return

        role = guild.get_role(entry.role_id)
        if role is None:
            logger.warning("Role %s missing for panel %s", entry.role_id, panel.id)
            return

        if adding:
            await assign_role(member, role)
        else:
            await remove_role(member, role)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_raw_reaction(payload, adding=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_raw_reaction(payload, adding=False)


async def setup(bot: commands.Bot) -> None:
    raise RuntimeError("Load ReactionRolesCog from bot.py with a shared Database instance.")
