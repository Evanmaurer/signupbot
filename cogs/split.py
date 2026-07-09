"""AVA loot split slash commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models import FarmEvent
from split_calculator import (
    SellOffLocation,
    build_ava_split_embed,
    calculate_ava_split,
    parse_silver_value,
)
from utils import find_events_for_context, is_event_host
from views.farm_view import FarmEventSelectView

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


class SplitCog(commands.Cog):
    """AVA loot split calculator integrated with farm signup events."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

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

    async def _resolve_event_or_reply(
        self,
        interaction: discord.Interaction,
        event_id: int | None,
    ) -> FarmEvent | None:
        matches = await find_events_for_context(self.db, interaction, event_id)

        if not matches:
            await interaction.response.send_message(
                "No farm event found for this channel or thread. "
                "Run `/avasplit` in the signup channel or signup thread.",
                ephemeral=True,
            )
            return None

        if len(matches) == 1:
            return matches[0]

        guild_matches = [e for e in matches if e.guild_id == interaction.guild_id]
        if len(guild_matches) == 1:
            return guild_matches[0]

        picker_events = [(e.id, e.title) for e in guild_matches or matches]
        view = FarmEventSelectView(picker_events)
        await interaction.response.send_message(
            "Multiple events found. Select one:",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.event_id is None:
            return None
        return await self.db.get_event_by_id(view.event_id)

    @app_commands.command(
        name="avasplit",
        description="Calculate AVA loot split payouts for a farm signup event.",
    )
    @app_commands.describe(
        total_item_value="Total item value in silver (e.g. 18500000 or 18,500,000)",
        total_repairs="Total repair costs in silver",
        total_silver_bags="Total silver from bags",
        map_cost="Map cost in silver",
        sell_off_location="Where the loot was sold",
        event_id="Event ID (optional — auto-detected from channel/thread)",
    )
    @app_commands.choices(
        sell_off_location=[
            app_commands.Choice(name="City (15% deduction)", value="city"),
            app_commands.Choice(name="Hideout (20% deduction)", value="hideout"),
        ]
    )
    async def avasplit(
        self,
        interaction: discord.Interaction,
        total_item_value: str,
        total_repairs: str,
        total_silver_bags: str,
        map_cost: str,
        sell_off_location: app_commands.Choice[str],
        event_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        if not interaction.response.is_done():
            await interaction.response.defer()

        try:
            item_value = parse_silver_value("Total Item Value", total_item_value)
            repairs = parse_silver_value("Total Repairs", total_repairs)
            silver_bags = parse_silver_value("Total Silver Bags", total_silver_bags)
            map_cost_value = parse_silver_value("Map Cost", map_cost)
            location = SellOffLocation.from_choice(sell_off_location.value)

            result = calculate_ava_split(
                event=event,
                total_item_value=item_value,
                total_repairs=repairs,
                total_silver_bags=silver_bags,
                map_cost=map_cost_value,
                sell_off_location=location,
            )
        except ValueError as exc:
            if interaction.response.is_done():
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)
            return

        channel_id = interaction.channel.id
        if isinstance(interaction.channel, discord.Thread) and interaction.channel.parent_id:
            channel_id = interaction.channel.parent_id

        await self.db.save_ava_split(
            result,
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=channel_id,
            calculated_by=interaction.user.id,
        )

        embed = build_ava_split_embed(result)
        await interaction.followup.send(embed=embed)

        logger.info(
            "AVA split for event %s: %s silver/player (%s players)",
            event.id,
            result.per_player,
            result.player_count,
        )


async def setup(bot: commands.Bot) -> None:
    """Cog loader stub — SplitCog is registered directly from bot.py."""
    raise RuntimeError("Load SplitCog from bot.py with a shared Database instance.")
