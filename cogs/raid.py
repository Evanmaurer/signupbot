"""AVA raid signups — Raid-Helper style presets, role dropdown and buttons."""

from __future__ import annotations

import asyncio
from pathlib import Path
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config
from models import FarmEvent, RaidPreset, Slot, SignupStatus
from raid_presets import (
    DEFAULT_PRESET_NAME,
    DEFAULT_PRESET_ROLES,
    DEFAULT_PRESET_TITLE,
    expand_roles,
    format_roles,
    parse_roles,
    roles_summary,
)
from utils import (
    build_farm_embed,
    combine_farm_datetime,
    discord_timestamp,
    find_events_for_context,
    get_event_timezone,
    is_event_host,
    signup_allowed_mentions,
    signup_ping_content,
)
from views.farm_view import FarmEventSelectView
from views.raid_view import DYNAMIC_ITEMS, build_raid_view

COMP_IMAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "raid_comp.png"
COMP_IMAGE_NAME = "raid_comp.png"

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

TIME_HELP = (
    "**How to enter time:**\n"
    "• **date:** `11/29` (this year is assumed)\n"
    f"• **time ({config.EVENT_TIMEZONE}):** `18`, `18:30` or `6 PM`"
)


# ── Preset modal ──────────────────────────────────────────────────────────────


class PresetModal(discord.ui.Modal):
    """Create or edit a raid preset."""

    def __init__(self, cog: RaidCog, existing: RaidPreset | None = None) -> None:
        super().__init__(title="Edit Raid Preset" if existing else "New Raid Preset")
        self.cog = cog
        self.existing = existing

        self.name_input = discord.ui.TextInput(
            label="Preset name",
            placeholder="e.g. AVA Fame Farm, AVA Chest Run",
            default=existing.name if existing else None,
            max_length=50,
        )
        self.roles_input = discord.ui.TextInput(
            label="Roles — one per line, use xN for multiples",
            style=discord.TextStyle.paragraph,
            placeholder="🛡️ Tank\n💚 Main Heal\n⚔️ DPS x5",
            default=existing.roles if existing else DEFAULT_PRESET_ROLES,
            max_length=1000,
        )
        for item in (self.name_input, self.roles_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.name_input.value.strip()
        try:
            groups = parse_roles(self.roles_input.value)
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}\n\n**Role format** — one per line:\n"
                "```\n🛡️ Tank\n💚 Main Heal\n⚔️ DPS x5\n```",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        assert guild_id is not None
        clash = await self.cog.db.get_preset(guild_id, name)
        renaming_onto_other = clash is not None and (
            self.existing is None or clash.id != self.existing.id
        )
        if renaming_onto_other:
            await interaction.response.send_message(
                f"❌ A preset named **{clash.name}** already exists. "
                f"Use `/ava preset edit` to change it.",
                ephemeral=True,
            )
            return

        await self.cog.db.save_preset(
            guild_id=guild_id,
            name=name,
            title=name,
            map_type="",
            roles=format_roles(groups),
            requirements="",
            creator_id=interaction.user.id,
            original_name=self.existing.name if self.existing else None,
        )
        total = sum(count for _, _, count in groups)
        verb = "Updated" if self.existing else "Saved"
        await interaction.response.send_message(
            f"✅ {verb} preset **{name}** ({total} slots)\n{roles_summary(groups)}\n\n"
            f"Post it with `/ava create preset:{name}`",
            ephemeral=True,
        )


def _has_comp_image(message: discord.Message | None) -> bool:
    return message is not None and any(
        a.filename == COMP_IMAGE_NAME for a in message.attachments
    )


# ── Cog ───────────────────────────────────────────────────────────────────────


class RaidCog(commands.Cog):
    """Raid-Helper style AVA raid signups."""

    raid = app_commands.Group(name="ava", description="Create and manage AVA raid signups.")
    preset = app_commands.Group(
        name="preset", description="Manage raid signup presets.", parent=raid
    )

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._locks: dict[int, asyncio.Lock] = {}
        self._refresh_task: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)
        self._refresh_task = asyncio.create_task(self._refresh_recent_raids())

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)
        if self._refresh_task:
            self._refresh_task.cancel()

    async def _refresh_recent_raids(self) -> None:
        """Re-render recent raid posts so older (thread-style) posts get the dropdown."""
        await self.bot.wait_until_ready()
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        refreshed = 0
        for event in await self.db.get_active_events():
            if event.start_time < cutoff:
                continue
            if await self._update_signup_message(event):
                refreshed += 1
        logger.info("Refreshed %d upcoming raid signup post(s)", refreshed)

    def _lock(self, event_id: int) -> asyncio.Lock:
        return self._locks.setdefault(event_id, asyncio.Lock())

    # ── helpers ──────────────────────────────────────────────────────────

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

    async def _ensure_default_preset(self, guild_id: int) -> list[RaidPreset]:
        presets = await self.db.get_presets(guild_id)
        if presets:
            return presets
        await self.db.save_preset(
            guild_id=guild_id,
            name=DEFAULT_PRESET_NAME,
            title=DEFAULT_PRESET_TITLE,
            map_type="",
            roles=DEFAULT_PRESET_ROLES,
            requirements="",
            creator_id=self.bot.user.id if self.bot.user else 0,
        )
        return await self.db.get_presets(guild_id)

    async def _update_signup_message(self, event: FarmEvent) -> bool:
        try:
            channel = self.bot.get_channel(event.channel_id) or await self.bot.fetch_channel(
                event.channel_id
            )
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return False
            message = await channel.fetch_message(event.message_id)
            await message.edit(
                embed=build_farm_embed(event, with_image=_has_comp_image(message)),
                view=build_raid_view(event),
            )
            return True
        except discord.HTTPException as exc:
            logger.warning("Failed to update raid post for event %s: %s", event.id, exc)
            return False

    async def _resolve_event(
        self, interaction: discord.Interaction, event_id: int | None
    ) -> FarmEvent | None:
        """Find the target raid; shows a picker if the channel has several."""
        if event_id is not None:
            event = await self.db.get_event_by_id(event_id)
            if event is None or event.guild_id != interaction.guild_id:
                await interaction.response.send_message("Raid not found.", ephemeral=True)
                return None
            return event

        matches = await find_events_for_context(self.db, interaction)
        if not matches:
            matches = await self.db.get_events_by_guild(interaction.guild_id or 0)
        if not matches:
            await interaction.response.send_message("No raids found.", ephemeral=True)
            return None
        if len(matches) == 1:
            return matches[0]

        matches = sorted(matches, key=lambda e: e.id, reverse=True)[:25]
        view = FarmEventSelectView([(e.id, f"#{e.id} · {e.title}") for e in matches])
        await interaction.response.send_message(
            "Which raid? (tip: use the `raid` option to skip this)", view=view, ephemeral=True
        )
        await view.wait()
        if view.event_id is None:
            return None
        return await self.db.get_event_by_id(view.event_id)

    async def _reply(self, interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    # ── autocomplete ─────────────────────────────────────────────────────

    async def preset_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        try:
            presets = await self._ensure_default_preset(interaction.guild_id)
        except Exception:
            logger.exception("Preset autocomplete failed")
            return []
        current = current.lower()
        choices = [
            app_commands.Choice(name=p.name[:100], value=p.name[:100])
            for p in presets
            if current in p.name.lower()
        ][:25]
        logger.info("Preset autocomplete %r -> %d choice(s)", current, len(choices))
        return choices

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        logger.info(
            "Interaction received: type=%s name=%s custom_id=%s user=%s",
            interaction.type.name,
            data.get("name"),
            data.get("custom_id"),
            interaction.user.id if interaction.user else None,
        )

    async def raid_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        if interaction.guild_id is None:
            return []
        events = await self.db.get_events_by_guild(interaction.guild_id)
        current = current.lower()
        choices: list[app_commands.Choice[int]] = []
        for event in events:
            label = (
                f"#{event.id} · {event.title} · "
                f"{event.start_time.strftime('%b %d %I:%M %p')}"
                f"{' (closed)' if event.closed else ''}"
            )
            if current and current not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=event.id))
            if len(choices) >= 25:
                break
        return choices

    async def role_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        event_id = getattr(interaction.namespace, "raid", None)
        events: list[FarmEvent] = []
        if isinstance(event_id, int):
            event = await self.db.get_event_by_id(event_id)
            events = [event] if event else []
        else:
            events = await find_events_for_context(self.db, interaction)
        names: list[str] = []
        for event in events:
            for group in event.role_groups():
                if group.role_name not in names:
                    names.append(group.role_name)
        current = current.lower()
        return [
            app_commands.Choice(name=n[:100], value=n[:100])
            for n in names
            if current in n.lower()
        ][:25]

    # ── signup interactions (called from views.raid_view) ────────────────

    async def handle_role_pick(
        self, interaction: discord.Interaction, event_id: int, role_name: str
    ) -> None:
        async with self._lock(event_id):
            event = await self.db.get_event_by_id(event_id)
            if event is None:
                await interaction.response.send_message(
                    "This raid no longer exists.", ephemeral=True
                )
                return

            user_id = interaction.user.id
            group = event.role_group(role_name)
            current = event.slot_for_user(user_id)
            message: str

            if event.closed:
                message = "🔒 Signups are closed for this raid."
            elif group is None:
                message = "That role doesn't exist on this raid anymore."
            elif current is not None and current.role_name == group.role_name:
                message = f"You're already signed up as **{group.emoji} {group.role_name}**."
            elif group.first_open() is None:
                message = (
                    f"**{group.emoji} {group.role_name}** is full. "
                    "Pick another role or mark yourself Tentative."
                )
            else:
                target = group.first_open()
                assert target is not None
                if current is not None:
                    await self.db.set_slot_user(event.id, current.slot_number, None)
                await self.db.set_slot_user(event.id, target.slot_number, user_id)
                await self.db.set_user_status(event.id, user_id, None)
                swapped = f" (moved from {current.role_name})" if current else ""
                message = f"✅ Signed up as **{group.emoji} {group.role_name}**{swapped}."
                event = await self.db.get_event_by_id(event.id) or event

            # Editing the message also resets the dropdown's selection.
            await interaction.response.edit_message(
                embed=build_farm_embed(
                    event, with_image=_has_comp_image(interaction.message)
                ),
                view=build_raid_view(event),
            )
        await interaction.followup.send(message, ephemeral=True)

    async def handle_action(
        self, interaction: discord.Interaction, event_id: int, action: str
    ) -> None:
        async with self._lock(event_id):
            event = await self.db.get_event_by_id(event_id)
            if event is None:
                await interaction.response.send_message(
                    "This raid no longer exists.", ephemeral=True
                )
                return
            if event.closed:
                await interaction.response.send_message(
                    "🔒 Signups are closed for this raid.", ephemeral=True
                )
                return

            user_id = interaction.user.id
            current = event.slot_for_user(user_id)
            status = event.statuses.get(user_id)

            if action == "leave":
                if current is None and status is None:
                    await interaction.response.send_message(
                        "You aren't signed up for this raid.", ephemeral=True
                    )
                    return
                message = "🚪 You've been removed from this raid."
                new_status = None
            elif status == action:
                message = f"Removed your **{action}** mark."
                new_status = None
            else:
                new_status = action
                message = (
                    "❔ Marked as **Tentative**."
                    if action == SignupStatus.TENTATIVE
                    else "🚫 Marked as **Absent**."
                )
                if current is not None:
                    message += f" Your **{current.role_name}** slot was freed."

            if current is not None:
                await self.db.set_slot_user(event.id, current.slot_number, None)
            await self.db.set_user_status(event.id, user_id, new_status)
            event = await self.db.get_event_by_id(event.id) or event

            await interaction.response.edit_message(
                embed=build_farm_embed(
                    event, with_image=_has_comp_image(interaction.message)
                ),
                view=build_raid_view(event),
            )
        await interaction.followup.send(message, ephemeral=True)

    # ── /ava preset ──────────────────────────────────────────────────────────

    @preset.command(name="create", description="Create a new raid preset (roles + defaults).")
    async def preset_create(self, interaction: discord.Interaction) -> None:
        if not await self._require_host(interaction):
            return
        await interaction.response.send_modal(PresetModal(self))

    @preset.command(name="edit", description="Edit an existing raid preset.")
    @app_commands.describe(name="Preset to edit")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_edit(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_host(interaction):
            return
        preset = await self.db.get_preset(interaction.guild_id or 0, name)
        if preset is None:
            await interaction.response.send_message(
                f"No preset named **{name}**.", ephemeral=True
            )
            return
        await interaction.response.send_modal(PresetModal(self, preset))

    @preset.command(name="list", description="Show this server's raid presets.")
    async def preset_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        presets = await self._ensure_default_preset(interaction.guild_id)
        embed = discord.Embed(title="📋 Raid Presets", colour=config.EMBED_COLOUR_OPEN)
        for preset in presets[:25]:
            try:
                groups = parse_roles(preset.roles)
                total = sum(count for _, _, count in groups)
                summary = roles_summary(groups)
            except ValueError:
                total, summary = 0, "_invalid roles — edit this preset_"
            embed.add_field(
                name=f"{preset.name}  ·  {total} slots",
                value=summary[:1024],
                inline=False,
            )
        embed.set_footer(text="/ava preset create · /ava preset edit · /ava create")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @preset.command(name="delete", description="Delete a raid preset.")
    @app_commands.describe(name="Preset to delete")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_delete(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_host(interaction):
            return
        deleted = await self.db.delete_preset(interaction.guild_id or 0, name)
        await interaction.response.send_message(
            f"🗑️ Deleted preset **{name}**." if deleted else f"No preset named **{name}**.",
            ephemeral=True,
        )

    # ── /ava ────────────────────────────────────────────────────────────

    @raid.command(name="create", description="Post an AVA raid signup from a preset.")
    @app_commands.describe(
        title="Name of the raid (shown at the top of the post)",
        preset="Which roster to use",
        date="Raid date — MM/DD, e.g. 11/29 (this year)",
        time="Raid time in UTC — e.g. 18, 18:30 or 6 PM",
        ping="Ping the raid role (default: yes)",
    )
    @app_commands.autocomplete(preset=preset_autocomplete)
    async def raid_create(
        self,
        interaction: discord.Interaction,
        title: str,
        preset: str,
        date: str,
        time: str,
        ping: bool = True,
    ) -> None:
        if not await self._require_host(interaction):
            return
        guild_id = interaction.guild_id
        assert guild_id is not None

        # Post in the configured raid channel, or here if none is set.
        settings = await self.db.get_guild_settings(guild_id)
        channel: discord.abc.Messageable | None = interaction.channel
        if settings.raid_channel_id:
            channel = self.bot.get_channel(settings.raid_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Set a raid channel with `/ava settings`, or run this in a text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._ensure_default_preset(guild_id)

        chosen = await self.db.get_preset(guild_id, preset)
        if chosen is None:
            await interaction.followup.send(
                f"No preset named **{preset}**. See `/ava preset list`.", ephemeral=True
            )
            return

        try:
            start = combine_farm_datetime(date, time)
            groups = parse_roles(chosen.roles)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}\n\n{TIME_HELP}", ephemeral=True)
            return

        slots = expand_roles(groups)
        final_title = title.strip()[:256]
        with_image = COMP_IMAGE_PATH.exists()

        preview = FarmEvent(
            id=0,
            guild_id=guild_id,
            channel_id=channel.id,
            message_id=0,
            thread_id=None,
            creator_id=interaction.user.id,
            title=final_title,
            leaving_from="",
            map_type="",
            start_time=start,
            requirements="",
            closed=False,
            slots=[Slot(slot_number=n, role_name=r, emoji=e) for n, r, e in slots],
        )

        try:
            message = await channel.send(
                content=signup_ping_content() if ping else None,
                embed=build_farm_embed(preview, with_image=with_image),
                file=(
                    discord.File(COMP_IMAGE_PATH, filename=COMP_IMAGE_NAME)
                    if with_image
                    else discord.utils.MISSING
                ),
                allowed_mentions=(
                    signup_allowed_mentions() if ping else discord.AllowedMentions.none()
                ),
            )
        except discord.HTTPException as exc:
            logger.error("Failed to post raid signup: %s", exc)
            await interaction.followup.send(f"Failed to post signup: {exc}", ephemeral=True)
            return

        event = await self.db.create_event(
            guild_id=guild_id,
            channel_id=channel.id,
            message_id=message.id,
            creator_id=interaction.user.id,
            title=final_title,
            leaving_from="",
            map_type="",
            start_time=start,
            requirements="",
            slots=slots,
        )
        # Now that we have a raid ID, attach the dropdown + buttons. Re-read first: if the
        # start time has already passed, the auto-start may have closed it meanwhile.
        event = await self.db.get_event_by_id(event.id) or event
        await message.edit(
            embed=build_farm_embed(event, with_image=with_image), view=build_raid_view(event)
        )

        await interaction.followup.send(
            f"✅ Raid **#{event.id}** posted: {message.jump_url}\n"
            f"**Starts:** {discord_timestamp(start, 'F')} ({discord_timestamp(start, 'R')})"
            + (
                "\n⚠️ That start time has already passed, so signups closed right away. "
                "Times are in **UTC** — use `/ava edit` to move it."
                if start <= datetime.now(timezone.utc)
                else ""
            ),
            ephemeral=True,
        )
        logger.info("Raid %s created by %s in guild %s", event.id, interaction.user.id, guild_id)

    @raid.command(name="edit", description="Edit a raid's details.")
    @app_commands.describe(
        raid="Raid to edit (optional if there's only one in this channel)",
        title="New title",
        date="New date — MM/DD, e.g. 11/29 (this year)",
        time="New time in UTC — e.g. 18, 18:30 or 6 PM",
    )
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_edit(
        self,
        interaction: discord.Interaction,
        raid: int | None = None,
        title: str | None = None,
        date: str | None = None,
        time: str | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return
        if not any([title, date, time]):
            await interaction.response.send_message(
                "Provide at least one field to change.", ephemeral=True
            )
            return

        event = await self._resolve_event(interaction, raid)
        if event is None:
            return

        start: datetime | None = None
        if date or time:
            local = event.start_time.astimezone(get_event_timezone())
            try:
                start = combine_farm_datetime(
                    date or local.strftime("%m/%d/%Y"), time or local.strftime("%H:%M")
                )
            except ValueError as exc:
                await self._reply(interaction, f"❌ {exc}\n\n{TIME_HELP}")
                return

        await self.db.update_event_fields(
            event.id,
            title=title,
            start_time=start,
        )
        if start is not None and start > datetime.now(timezone.utc):
            # Moved into the future: signups reopen and it will auto-close again then.
            await self.db.set_started(event.id, False)
            if event.closed and not event.loot_thread_id:
                await self.db.set_closed(event.id, False)
        refreshed = await self.db.get_event_by_id(event.id)
        if refreshed:
            await self._update_signup_message(refreshed)
        await self._reply(interaction, f"✏️ Updated raid **#{event.id}**.")

    # ── /ava settings & /ava loot ────────────────────────────────────────

    @raid.command(name="settings", description="Set where raids are posted and loot threads open.")
    @app_commands.describe(
        raid_channel="Channel every /ava create posts into",
        loot_channel="Channel where a loot thread opens when each raid starts",
    )
    async def ava_settings(
        self,
        interaction: discord.Interaction,
        raid_channel: discord.TextChannel | None = None,
        loot_channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return
        guild_id = interaction.guild_id or 0
        if raid_channel is not None or loot_channel is not None:
            await self.db.update_guild_settings(
                guild_id,
                raid_channel_id=raid_channel.id if raid_channel else None,
                loot_channel_id=loot_channel.id if loot_channel else None,
            )
        settings = await self.db.get_guild_settings(guild_id)

        def show(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "_not set_"

        await interaction.response.send_message(
            "⚙️ **AVA settings**\n"
            f"📣 Raid posts: {show(settings.raid_channel_id)}"
            + ("" if settings.raid_channel_id else " _(posts where you run /ava create)_")
            + f"\n💰 Loot threads: {show(settings.loot_channel_id)}\n\n"
            "When a raid's start time passes, signups close and its loot thread opens.",
            ephemeral=True,
        )

    @raid.command(name="loot", description="Close signups and open a raid's loot thread now.")
    @app_commands.describe(raid="Raid (optional if there's only one in this channel)")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def ava_loot(self, interaction: discord.Interaction, raid: int | None = None) -> None:
        if not await self._require_host(interaction):
            return
        event = await self._resolve_event(interaction, raid)
        if event is None:
            return
        loot_cog = self.bot.get_cog("LootCog")
        if loot_cog is None:
            await self._reply(interaction, "Loot threads aren't available right now.")
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        message = await loot_cog.start_raid(event)  # type: ignore[attr-defined]
        await self._reply(interaction, message)

    async def _set_closed(
        self, interaction: discord.Interaction, raid: int | None, closed: bool
    ) -> None:
        if not await self._require_host(interaction):
            return
        event = await self._resolve_event(interaction, raid)
        if event is None:
            return
        await self.db.set_closed(event.id, closed)
        event.closed = closed
        await self._update_signup_message(event)
        state = "🔒 closed" if closed else "🟢 reopened"
        await self._reply(interaction, f"Signups {state} for **{event.title}** (#{event.id}).")

    @raid.command(name="close", description="Close signups for a raid.")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_close(self, interaction: discord.Interaction, raid: int | None = None) -> None:
        await self._set_closed(interaction, raid, True)

    @raid.command(name="reopen", description="Reopen signups for a raid.")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_reopen(self, interaction: discord.Interaction, raid: int | None = None) -> None:
        await self._set_closed(interaction, raid, False)

    @raid.command(name="delete", description="Delete a raid signup post and its data.")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_delete(self, interaction: discord.Interaction, raid: int | None = None) -> None:
        if not await self._require_host(interaction):
            return
        event = await self._resolve_event(interaction, raid)
        if event is None:
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        for target_id, is_thread in ((event.channel_id, False), (event.thread_id, True)):
            if not target_id:
                continue
            try:
                channel = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
                if is_thread and isinstance(channel, discord.Thread):
                    await channel.delete()
                elif not is_thread and isinstance(channel, (discord.TextChannel, discord.Thread)):
                    await channel.get_partial_message(event.message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                logger.warning("Could not clean up raid %s: %s", event.id, exc)

        await self.db.delete_event(event.id)
        await self._reply(interaction, f"🗑️ Deleted raid **{event.title}** (#{event.id}).")

    @raid.command(name="add", description="Put a member into a role on a raid.")
    @app_commands.describe(user="Member to add", role="Role to put them in", raid="Raid")
    @app_commands.autocomplete(role=role_autocomplete, raid=raid_autocomplete)
    async def raid_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: str,
        raid: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return
        event = await self._resolve_event(interaction, raid)
        if event is None:
            return

        async with self._lock(event.id):
            event = await self.db.get_event_by_id(event.id) or event
            group = event.role_group(role)
            if group is None:
                await self._reply(interaction, f"Raid #{event.id} has no role **{role}**.")
                return
            current = event.slot_for_user(user.id)
            if current is not None and current.role_name == group.role_name:
                await self._reply(interaction, f"{user.mention} is already **{group.role_name}**.")
                return
            target = group.first_open()
            if target is None:
                await self._reply(
                    interaction,
                    f"**{group.role_name}** is full — `/ava remove` someone first.",
                )
                return
            if current is not None:
                await self.db.set_slot_user(event.id, current.slot_number, None)
            await self.db.set_slot_user(event.id, target.slot_number, user.id)
            await self.db.set_user_status(event.id, user.id, None)
            refreshed = await self.db.get_event_by_id(event.id)

        if refreshed:
            await self._update_signup_message(refreshed)
        await self._reply(interaction, f"✅ Added {user.mention} as **{group.role_name}**.")

    @raid.command(name="remove", description="Remove a member from a raid.")
    @app_commands.describe(user="Member to remove", raid="Raid")
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        raid: int | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return
        event = await self._resolve_event(interaction, raid)
        if event is None:
            return

        async with self._lock(event.id):
            freed = await self.db.clear_user_from_event(event.id, user.id)
            had_status = user.id in event.statuses
            await self.db.set_user_status(event.id, user.id, None)
            refreshed = await self.db.get_event_by_id(event.id)

        if freed is None and not had_status:
            await self._reply(interaction, f"{user.display_name} isn't signed up.")
            return
        if refreshed:
            await self._update_signup_message(refreshed)
        await self._reply(interaction, f"🚪 Removed {user.mention} from raid #{event.id}.")

    @raid.command(name="list", description="List raids posted in this server.")
    async def raid_list(self, interaction: discord.Interaction) -> None:
        events = await self.db.get_events_by_guild(interaction.guild_id or 0)
        now = datetime.now(timezone.utc) - timedelta(hours=6)
        upcoming = [e for e in events if e.start_time >= now]
        if not upcoming:
            await interaction.response.send_message("No upcoming raids.", ephemeral=True)
            return
        lines = [
            f"**#{e.id}** {e.title} — {discord_timestamp(e.start_time, 'f')} · "
            f"{e.filled_slots}/{e.total_slots}{' 🔒' if e.closed else ''} · "
            f"https://discord.com/channels/{e.guild_id}/{e.channel_id}/{e.message_id}"
            for e in sorted(upcoming, key=lambda e: e.start_time)[:15]
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Cog loader stub — RaidCog is registered directly from bot.py."""
    raise RuntimeError("Load RaidCog from bot.py with a shared Database instance.")
