"""Fame farm signup cog — slash commands and thread-based signups."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config
from models import FarmEvent, Slot
from utils import (
    build_farm_embed,
    combine_farm_datetime,
    discord_timestamp,
    get_default_farm_slots,
    is_event_host,
    parse_signup_message,
    react_outcome,
    resolve_event_from_interaction,
    safe_delete_message,
    signup_allowed_mentions,
    signup_ping_content,
)
from views.farm_view import FarmEventSelectView

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


class SignupResult:
    """Outcome of processing a thread signup message."""

    __slots__ = ("success", "reply")

    def __init__(self, success: bool, reply: str | None = None) -> None:
        self.success = success
        self.reply = reply


class FarmCog(commands.Cog):
    """Slash commands and signup handling for fame farm events."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._cooldowns: dict[tuple[int, int], float] = {}

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
        event = await resolve_event_from_interaction(self.db, interaction, event_id)
        if event is not None:
            return event

        events = await self.db.get_active_events()
        guild_events = [
            (event.id, event.title)
            for event in events
            if event.guild_id == interaction.guild_id
        ]
        if not guild_events:
            await interaction.response.send_message(
                "No active farm events found.", ephemeral=True
            )
            return None

        if len(guild_events) == 1:
            return await self.db.get_event_by_id(guild_events[0][0])

        view = FarmEventSelectView(guild_events)
        await interaction.response.send_message(
            "Multiple active events found. Select one:",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.event_id is None:
            return None
        return await self.db.get_event_by_id(view.event_id)

    async def _update_signup_message(self, event: FarmEvent) -> None:
        channel = self.bot.get_channel(event.channel_id)
        if not isinstance(channel, discord.TextChannel):
            channel = await self.bot.fetch_channel(event.channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.error("Channel %s is not a text channel", event.channel_id)
            return

        try:
            message = await channel.fetch_message(event.message_id)
            await message.edit(embed=build_farm_embed(event))
        except discord.HTTPException as exc:
            logger.error(
                "Failed to update signup message for event %s: %s", event.id, exc
            )

    async def _defer_if_needed(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _ephemeral_followup(
        self, interaction: discord.Interaction, content: str
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    @app_commands.command(
        name="farm",
        description="Create a new Fame Farm signup announcement.",
    )
    @app_commands.describe(
        title="Event title (e.g. ⚔️ Fame Farm Sign-up (AVA))",
        leaving_from="Where the group is leaving from",
        map_type="Map tier/type (e.g. 8.3)",
        date="Raid date — MM/DD/YYYY (example: 6/29/2026)",
        time="Raid time — 1:00 PM or 13:00 (your local time)",
        requirements="Event requirements (one per line)",
    )
    async def farm(
        self,
        interaction: discord.Interaction,
        title: str,
        leaving_from: str,
        map_type: str,
        date: str,
        time: str,
        requirements: str,
    ) -> None:
        if not await self._require_host(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            parsed_start = combine_farm_datetime(date, time)
        except ValueError as exc:
            await interaction.followup.send(
                f"{exc}\n\n**How to enter time:**\n"
                f"• **date:** `6/29/2026`\n"
                f"• **time:** `1:00 PM` or `13:00`\n"
                f"• Set your timezone in `.env` → `EVENT_TIMEZONE={config.EVENT_TIMEZONE}`",
                ephemeral=True,
            )
            return

        slots = get_default_farm_slots()

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                "This command must be used in a text channel.", ephemeral=True
            )
            return

        preview = FarmEvent(
            id=0,
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=interaction.channel.id,
            message_id=0,
            thread_id=0,
            creator_id=interaction.user.id,
            title=title,
            leaving_from=leaving_from,
            map_type=map_type,
            start_time=parsed_start,
            requirements=requirements,
            closed=False,
            slots=[
                Slot(slot_number=number, role_name=role) for number, role in slots
            ],
        )

        try:
            message = await interaction.channel.send(
                content=signup_ping_content(),
                embed=build_farm_embed(preview),
                allowed_mentions=signup_allowed_mentions(),
            )
            thread_name = title if len(title) <= 100 else f"{title[:97]}..."
            if "signup" not in title.lower():
                suffix = " Signups"
                thread_name = (
                    f"{title[: 100 - len(suffix)]}{suffix}"
                    if len(title) + len(suffix) > 100
                    else f"{title}{suffix}"
                )
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=10080,
            )
        except discord.HTTPException as exc:
            logger.error("Failed to create signup message/thread: %s", exc)
            await interaction.followup.send(
                f"Failed to create signup: {exc}", ephemeral=True
            )
            return

        event = await self.db.create_event(
            guild_id=interaction.guild_id,  # type: ignore[arg-type]
            channel_id=interaction.channel.id,
            message_id=message.id,
            thread_id=thread.id,
            creator_id=interaction.user.id,
            title=title,
            leaving_from=leaving_from,
            map_type=map_type,
            start_time=parsed_start,
            requirements=requirements,
            slots=slots,
        )

        await interaction.followup.send(
            f"Signup created in {message.jump_url}\n\n"
            f"**Raid starts:** {discord_timestamp(parsed_start, 'F')}\n"
            f"**Countdown:** {discord_timestamp(parsed_start, 'R')}\n"
            f"_Discord adjusts these for each person's timezone automatically._",
            ephemeral=True,
        )
        logger.info(
            "Event %s created by %s in guild %s", event.id, interaction.user.id, event.guild_id
        )

    @app_commands.command(name="closefarm", description="Close signups for a farm event.")
    @app_commands.describe(event_id="Event ID (optional if used inside the signup thread)")
    async def closefarm(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        await self._defer_if_needed(interaction)
        await self.db.set_closed(event.id, True)
        event.closed = True
        await self._update_signup_message(event)

        await self._ephemeral_followup(interaction, f"Signups closed for **{event.title}**.")

    @app_commands.command(name="reopenfarm", description="Reopen signups for a farm event.")
    @app_commands.describe(event_id="Event ID (optional if used inside the signup thread)")
    async def reopenfarm(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        await self._defer_if_needed(interaction)
        await self.db.set_closed(event.id, False)
        event.closed = False
        await self._update_signup_message(event)

        await self._ephemeral_followup(interaction, f"Signups reopened for **{event.title}**.")

    @app_commands.command(
        name="deletefarm",
        description="Delete a farm signup, its thread, and database entry.",
    )
    @app_commands.describe(event_id="Event ID (optional if used inside the signup thread)")
    async def deletefarm(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        await self._defer_if_needed(interaction)

        try:
            channel = self.bot.get_channel(event.channel_id) or await self.bot.fetch_channel(
                event.channel_id
            )
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(event.message_id)
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException as exc:
                    logger.warning("Could not delete signup message: %s", exc)

            thread = self.bot.get_channel(event.thread_id) or await self.bot.fetch_channel(
                event.thread_id
            )
            if isinstance(thread, discord.Thread):
                try:
                    await thread.delete()
                except discord.HTTPException as exc:
                    logger.warning("Could not delete signup thread: %s", exc)
        except discord.HTTPException as exc:
            logger.error("Error deleting Discord resources for event %s: %s", event.id, exc)

        await self.db.delete_event(event.id)
        await interaction.followup.send(
            f"Deleted farm event **{event.title}**.", ephemeral=True
        )

    @app_commands.command(
        name="forcesignup",
        description="Force-assign a user to a slot.",
    )
    @app_commands.describe(
        user="The user to assign",
        slot="Slot number",
        event_id="Event ID (optional if used inside the signup thread)",
    )
    async def forcesignup(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        slot: int,
        event_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        target_slot = event.slot_by_number(slot)
        if target_slot is None:
            await self._defer_if_needed(interaction)
            await self._ephemeral_followup(interaction, "Invalid slot.")
            return

        await self._defer_if_needed(interaction)
        await self.db.clear_user_from_event(event.id, user.id)
        if target_slot.user_id is not None and target_slot.user_id != user.id:
            await self.db.set_slot_user(event.id, slot, None)

        await self.db.set_slot_user(event.id, slot, user.id)

        refreshed = await self.db.get_event_by_id(event.id)
        if refreshed:
            await self._update_signup_message(refreshed)

        await self._ephemeral_followup(
            interaction, f"Assigned {user.mention} to slot {slot}."
        )

    @app_commands.command(
        name="forceremove",
        description="Force-remove a user from their slot.",
    )
    @app_commands.describe(
        user="The user to remove",
        event_id="Event ID (optional if used inside the signup thread)",
    )
    async def forceremove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        event_id: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        await self._defer_if_needed(interaction)
        removed_slot = await self.db.clear_user_from_event(event.id, user.id)
        if removed_slot is None:
            await self._ephemeral_followup(interaction, f"{user.display_name} is not signed up.")
            return

        refreshed = await self.db.get_event_by_id(event.id)
        if refreshed:
            await self._update_signup_message(refreshed)

        await self._ephemeral_followup(
            interaction, f"Removed {user.mention} from slot {removed_slot}."
        )

    @app_commands.command(
        name="editfarm",
        description="Edit an existing farm signup without recreating it.",
    )
    @app_commands.describe(
        title="New event title",
        leaving_from="New leaving location",
        map_type="New map type",
        date="New date — MM/DD/YYYY (example: 6/29/2026)",
        time="New time — 1:00 PM or 13:00",
        requirements="New requirements (one per line)",
        event_id="Event ID (optional if used inside the signup thread)",
    )
    async def editfarm(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
        title: str | None = None,
        leaving_from: str | None = None,
        map_type: str | None = None,
        date: str | None = None,
        time: str | None = None,
        requirements: str | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return

        if not any([title, leaving_from, map_type, date, time, requirements]):
            await interaction.response.send_message(
                "Provide at least one field to update.", ephemeral=True
            )
            return

        if (date is None) != (time is None):
            await interaction.response.send_message(
                "Provide both **date** and **time** together when changing the start.",
                ephemeral=True,
            )
            return

        event = await self._resolve_event_or_reply(interaction, event_id)
        if event is None:
            return

        parsed_start: datetime | None = None
        if date is not None and time is not None:
            try:
                parsed_start = combine_farm_datetime(date, time)
            except ValueError as exc:
                await self._defer_if_needed(interaction)
                await self._ephemeral_followup(interaction, str(exc))
                return

        await self._defer_if_needed(interaction)
        await self.db.update_event_fields(
            event.id,
            title=title,
            leaving_from=leaving_from,
            map_type=map_type,
            start_time=parsed_start,
            requirements=requirements,
        )

        refreshed = await self.db.get_event_by_id(event.id)
        if refreshed:
            await self._update_signup_message(refreshed)

        await self._ephemeral_followup(interaction, f"Updated **{refreshed.title if refreshed else event.title}**.")

    def _on_cooldown(self, event_id: int, user_id: int) -> bool:
        key = (event_id, user_id)
        now = time.monotonic()
        last = self._cooldowns.get(key, 0.0)
        if now - last < config.SIGNUP_COOLDOWN_SECONDS:
            return True
        self._cooldowns[key] = now
        return False

    async def _process_signup(
        self,
        event: FarmEvent,
        user: discord.Member | discord.User,
        action: str,
        slot_number: int,
        *,
        is_admin: bool,
    ) -> SignupResult:
        if event.closed:
            return SignupResult(False, "Signups are closed.")

        target_slot = event.slot_by_number(slot_number)
        if target_slot is None:
            return SignupResult(False, "Invalid slot.")

        if action == "remove":
            if target_slot.user_id is None:
                return SignupResult(False, None)
            if target_slot.user_id != user.id and not is_admin:
                return SignupResult(False, None)
            await self.db.set_slot_user(event.id, slot_number, None)
            return SignupResult(True)

        # claim
        if target_slot.user_id is not None and target_slot.user_id != user.id:
            return SignupResult(False, "That slot is already taken.")

        existing = event.slot_for_user(user.id)
        if existing is not None and existing.slot_number != slot_number:
            await self.db.set_slot_user(event.id, existing.slot_number, None)

        await self.db.set_slot_user(event.id, slot_number, user.id)
        return SignupResult(True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        event = await self.db.get_event_by_thread(message.channel.id)
        if event is None:
            return

        parsed = parse_signup_message(message.content)
        if parsed is None:
            return

        action, slot_number = parsed

        if self._on_cooldown(event.id, message.author.id):
            asyncio.create_task(safe_delete_message(message))
            return

        is_admin = (
            isinstance(message.author, discord.Member)
            and (
                message.author.guild_permissions.administrator
                or is_event_host(message.author)
            )
        )

        result = await self._process_signup(
            event,
            message.author,
            action,
            slot_number,
            is_admin=is_admin,
        )

        await react_outcome(message, result.success)

        if result.reply:
            try:
                reply = await message.reply(result.reply, mention_author=False)
                asyncio.create_task(safe_delete_message(reply))
            except discord.HTTPException as exc:
                logger.warning("Could not send signup reply: %s", exc)

        if result.success:
            refreshed = await self.db.get_event_by_id(event.id)
            if refreshed:
                await self._update_signup_message(refreshed)

        asyncio.create_task(safe_delete_message(message))


async def setup(bot: commands.Bot) -> None:
    """Cog loader stub — FarmCog is registered directly from bot.py."""
    raise RuntimeError("Load FarmCog from bot.py with a shared Database instance.")
