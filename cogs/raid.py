"""AVA raid signups — Raid-Helper style presets, role dropdown and buttons."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from models import FarmEvent, RaidPreset, Slot
from raid_presets import (
    DEFAULT_PRESET_NAME,
    DEFAULT_PRESET_ROLES,
    DEFAULT_PRESET_TITLE,
    default_role_emoji,
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
    parse_farm_date,
    is_event_host,
    resolve_ping_role_id,
    server_timezone,
    signup_allowed_mentions,
    signup_ping_content,
)
from preset_media import (
    ICON_EMOJI_PREFIX,
    copy_image,
    delete_image,
    format_colour,
    icon_emoji_name,
    icon_image_bytes,
    parse_colour,
    posted_image,
    posted_image_name,
    raid_image_for,
    resolve_server_emojis,
    store_image,
)
from views.farm_view import FarmEventSelectView
from views.raid_view import DYNAMIC_ITEMS, build_raid_view


if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

# /overview also lists raids that started this recently (shown as in progress).
OVERVIEW_IN_PROGRESS = timedelta(hours=3)

# Choices for /ava settings timezone (the server's default for times typed without one).
TIMEZONE_CHOICES: list[tuple[str, str]] = [
    ("Eastern (New York)", "America/New_York"),
    ("Central (Chicago)", "America/Chicago"),
    ("Mountain (Denver)", "America/Denver"),
    ("Arizona (no DST)", "America/Phoenix"),
    ("Pacific (Los Angeles)", "America/Los_Angeles"),
    ("Alaska", "America/Anchorage"),
    ("Hawaii", "Pacific/Honolulu"),
    ("Atlantic (Halifax)", "America/Halifax"),
    ("Brazil (São Paulo)", "America/Sao_Paulo"),
    ("UTC / Albion server time", "UTC"),
    ("UK (London)", "Europe/London"),
    ("Central Europe (Paris/Berlin)", "Europe/Paris"),
    ("Eastern Europe (Athens)", "Europe/Athens"),
    ("Moscow", "Europe/Moscow"),
    ("India", "Asia/Kolkata"),
    ("Singapore / Philippines", "Asia/Singapore"),
    ("Japan / Korea", "Asia/Tokyo"),
    ("Australia East (Sydney)", "Australia/Sydney"),
    ("Australia West (Perth)", "Australia/Perth"),
    ("New Zealand", "Pacific/Auckland"),
]
_TIMEZONE_LABELS = {value: label for label, value in TIMEZONE_CHOICES}


def timezone_label(name: str | None) -> str:
    name = name or config.EVENT_TIMEZONE
    return _TIMEZONE_LABELS.get(name, name)


def time_help(tz_name: str | None) -> str:
    return (
        "**How to enter time:**\n"
        "• **time:** `10pm`, `9:30 PM`, `22:00` — read as the server timezone "
        f"(**{timezone_label(tz_name)}**). Add a timezone for any other: `10 PM CST`, "
        "`18 UTC`, `8pm UTC+2`\n"
        "• **date:** optional — `11/29`. Leave it out for **today**.\n"
        "Hosts can change the server timezone with `/ava settings timezone:`."
    )


def _read_as(start: datetime) -> str:
    """How the bot understood a typed time, e.g. '10:00 PM EST, Fri Nov 20'."""
    return start.strftime("%I:%M %p %Z, %a %b %d").lstrip("0")


# ── Preset modal ──────────────────────────────────────────────────────────────


class PresetModal(discord.ui.Modal):
    """Create or edit a raid preset."""

    def __init__(
        self,
        cog: RaidCog,
        existing: RaidPreset | None = None,
        *,
        default_name: str | None = None,
        default_roles: str | None = None,
        default_description: str | None = None,
        default_colour: int | None = None,
        copy_image_from: str | None = None,
        source_attachment: discord.Attachment | None = None,
    ) -> None:
        """Edit `existing`, or create a new preset pre-filled with the given defaults.

        copy_image_from (a stored preset image) or source_attachment (a raid post's
        picture) become the new preset's image unless the user uploads another.
        """
        super().__init__(title="Edit Raid Preset" if existing else "New Raid Preset")
        self.cog = cog
        self.existing = existing
        self.copy_image_from = copy_image_from
        self.source_attachment = source_attachment

        if existing:
            default_name, default_roles = existing.name, existing.roles
            default_description, default_colour = existing.requirements, existing.colour
        has_image = bool(
            (existing and existing.image_path) or copy_image_from or source_attachment
        )

        self.name_input = discord.ui.TextInput(
            label="Preset name",
            placeholder="e.g. AVA Fame Farm, AVA Chest Run",
            default=default_name[:50] if default_name else None,
            max_length=50,
        )
        self.roles_input = discord.ui.TextInput(
            label="Roles — one per line, use xN for multiples",
            style=discord.TextStyle.paragraph,
            placeholder="🛡️ Tank\n💚 Main Heal\n:server_emoji: DPS x5",
            default=(default_roles or DEFAULT_PRESET_ROLES)[:1000],
            max_length=1000,
        )
        self.description_input = discord.ui.TextInput(
            label="Description (optional) — shown on the post",
            style=discord.TextStyle.paragraph,
            placeholder="e.g. Bring T8 gear, meet at Fort Sterling portal. Voice required.",
            default=(default_description or "")[:1000] or None,
            required=False,
            max_length=1000,
        )
        self.colour_input = discord.ui.TextInput(
            label="Colour (optional) — hex or name",
            placeholder="#57F287, red, gold, purple, blue…",
            default=format_colour(default_colour) or None,
            required=False,
            max_length=20,
        )
        self.image_upload = discord.ui.FileUpload(required=False, max_values=1)
        image_label = discord.ui.Label(
            text="Raid image (optional)",
            description=(
                "Leave empty to keep the current image."
                if has_image
                else "Shown at the bottom of the post. Leave empty for the default."
            ),
            component=self.image_upload,
        )
        for item in (
            self.name_input,
            self.roles_input,
            self.description_input,
            self.colour_input,
            image_label,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.name_input.value.strip()
        roles_text = resolve_server_emojis(self.roles_input.value, interaction.guild)
        try:
            groups = parse_roles(roles_text)
            colour = parse_colour(self.colour_input.value)
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

        await interaction.response.defer(ephemeral=True)
        old_image = self.existing.image_path if self.existing else None
        try:
            if self.image_upload.values:
                image_path = await store_image(self.image_upload.values[0])
            elif self.existing:
                image_path = old_image
            elif self.copy_image_from:
                image_path = copy_image(self.copy_image_from)
            elif self.source_attachment:
                image_path = await store_image(self.source_attachment)
            else:
                image_path = None
        except (ValueError, discord.HTTPException, aiohttp.ClientError) as exc:
            await interaction.followup.send(f"❌ Image not saved: {exc}", ephemeral=True)
            return

        await self.cog.db.save_preset(
            guild_id=guild_id,
            name=name,
            title=name,
            map_type="",
            roles=format_roles(groups),
            requirements=self.description_input.value.strip(),
            # Keep the original owner when someone else (a host) edits it.
            creator_id=self.existing.creator_id if self.existing else interaction.user.id,
            original_name=self.existing.name if self.existing else None,
            image_path=image_path,
            colour=colour,
        )
        if old_image and old_image != image_path:
            delete_image(old_image)

        total = sum(count for _, _, count in groups)
        verb = "Updated" if self.existing else "Saved"
        extras = [
            label
            for label, present in (
                ("🖼️ custom image", image_path),
                ("📝 description", self.description_input.value.strip()),
                (f"🎨 {format_colour(colour)}", colour is not None),
            )
            if present
        ]
        await interaction.followup.send(
            f"✅ {verb} preset **{name}** ({total} slots)\n{roles_summary(groups)}"
            + (f"\n{' · '.join(extras)}" if extras else "")
            + f"\n\nPost it with `/ava create preset:{name}` · "
            f"add role pictures with `/ava preset icon`",
            ephemeral=True,
        )


class PresetDeleteView(discord.ui.View):
    """Private multi-select for deleting several presets at once."""

    def __init__(self, cog: RaidCog, user_id: int, presets: list[RaidPreset]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.presets = {str(p.id): p for p in presets}
        self.chosen: list[str] = []

        self.select = discord.ui.Select(
            placeholder="Choose presets to delete…",
            min_values=1,
            max_values=len(presets),
            options=[
                discord.SelectOption(
                    label=p.name[:100],
                    value=str(p.id),
                    description=_preset_option_description(p),
                )
                for p in presets
            ],
        )
        self.select.callback = self._picked
        self.add_item(self.select)

        self.delete_button = discord.ui.Button(
            label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=True
        )
        self.delete_button.callback = self._delete
        self.add_item(self.delete_button)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.chosen = list(self.select.values)
        for option in self.select.options:
            option.default = option.value in self.chosen  # keep the ticks visible
        self.delete_button.disabled = not self.chosen
        self.delete_button.label = f"Delete {len(self.chosen)}"
        names = ", ".join(f"**{self.presets[i].name}**" for i in self.chosen)
        await interaction.response.edit_message(
            content=f"🗑️ Delete {names}? This can't be undone.", view=self
        )

    async def _delete(self, interaction: discord.Interaction) -> None:
        deleted = await self.cog._delete_presets([self.presets[i] for i in self.chosen])
        self.stop()
        await interaction.response.edit_message(
            content=(
                "🗑️ Deleted " + ", ".join(f"**{n}**" for n in deleted) + "."
                if deleted
                else "Nothing was deleted — those presets were already gone."
            ),
            view=None,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing deleted.", view=None)


def _preset_option_description(preset: RaidPreset) -> str:
    try:
        groups = parse_roles(preset.roles)
        roles = ", ".join(name for _, name, _ in groups)
        return f"{sum(c for _, _, c in groups)} slots · {roles}"[:100]
    except ValueError:
        return "invalid roles"


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
        # Right-click a raid post → Apps → "Save as Raid Preset".
        self._save_preset_menu = app_commands.ContextMenu(
            name="Save as Raid Preset", callback=self.save_raid_as_preset
        )

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)
        self.bot.tree.add_command(self._save_preset_menu)
        self._refresh_task = asyncio.create_task(self._refresh_recent_raids())

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)
        self.bot.tree.remove_command(
            self._save_preset_menu.name, type=self._save_preset_menu.type
        )
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

    async def _require_member(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return False
        return True

    async def _get_owned_preset(
        self, interaction: discord.Interaction, name: str
    ) -> RaidPreset | None:
        """Fetch a preset the user may change: their own, or any if they're a host."""
        if not await self._require_member(interaction):
            return None
        preset = await self.db.get_preset(interaction.guild_id or 0, name)
        if preset is None:
            await interaction.response.send_message(
                f"No preset named **{name}**.", ephemeral=True
            )
            return None
        assert isinstance(interaction.user, discord.Member)
        if preset.creator_id != interaction.user.id and not is_event_host(interaction.user):
            await interaction.response.send_message(
                f"**{preset.name}** belongs to <@{preset.creator_id}>. Only they or an "
                f"Event Host can change it — use `/ava preset copy` to make your own version.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        return preset

    async def save_raid_as_preset(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """Context menu: open the preset form pre-filled from a posted raid's roles."""
        if not await self._require_member(interaction):
            return
        event = await self.db.get_event_by_message(message.id)
        if event is None or event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "That message isn't a raid signup post.", ephemeral=True
            )
            return
        groups = [
            (
                group.slots[0].emoji or default_role_emoji(group.role_name),
                group.role_name,
                group.total,
            )
            for group in event.role_groups()
        ]
        await interaction.response.send_modal(
            PresetModal(
                self,
                default_name=event.title,
                default_roles=format_roles(groups),
                default_description=event.requirements,
                default_colour=event.colour,
                source_attachment=posted_image(message),
            )
        )

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
                embed=build_farm_embed(event, image_name=posted_image_name(message)),
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
                    "Pick another role."
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
                    event, image_name=posted_image_name(interaction.message)
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
            elif action == "tentative":
                # Old posts may still show the removed Tentative button: re-render them.
                await interaction.response.edit_message(
                    embed=build_farm_embed(
                        event, image_name=posted_image_name(interaction.message)
                    ),
                    view=build_raid_view(event),
                )
                await interaction.followup.send(
                    "Tentative isn't used anymore — pick a role from the dropdown, "
                    "or press **Absence** if you can't make it.",
                    ephemeral=True,
                )
                return
            elif status == action:
                message = f"Removed your **{action}** mark."
                new_status = None
            else:
                new_status = action
                message = "🚫 Marked as **Absent**."
                if current is not None:
                    message += f" Your **{current.role_name}** slot was freed."

            if current is not None:
                await self.db.set_slot_user(event.id, current.slot_number, None)
            await self.db.set_user_status(event.id, user_id, new_status)
            event = await self.db.get_event_by_id(event.id) or event

            await interaction.response.edit_message(
                embed=build_farm_embed(
                    event, image_name=posted_image_name(interaction.message)
                ),
                view=build_raid_view(event),
            )
        await interaction.followup.send(message, ephemeral=True)

    # ── /ava preset ──────────────────────────────────────────────────────────

    @preset.command(name="create", description="Create a new raid preset (roles + defaults).")
    async def preset_create(self, interaction: discord.Interaction) -> None:
        if not await self._require_member(interaction):
            return
        await interaction.response.send_modal(PresetModal(self))

    @preset.command(name="copy", description="Make your own preset starting from an existing one.")
    @app_commands.describe(name="Preset to copy")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_copy(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_member(interaction):
            return
        source = await self.db.get_preset(interaction.guild_id or 0, name)
        if source is None:
            await interaction.response.send_message(
                f"No preset named **{name}**.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            PresetModal(
                self,
                default_name=f"{source.name[:43]} (copy)",
                default_roles=source.roles,
                default_description=source.requirements,
                default_colour=source.colour,
                copy_image_from=source.image_path,
            )
        )

    @preset.command(name="edit", description="Edit a raid preset you made.")
    @app_commands.describe(name="Preset to edit")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_edit(self, interaction: discord.Interaction, name: str) -> None:
        preset = await self._get_owned_preset(interaction, name)
        if preset is None:
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
            extras = " · ".join(
                label
                for label, present in (
                    ("🖼️ image", preset.image_path),
                    ("📝 description", preset.requirements.strip()),
                    (f"🎨 {format_colour(preset.colour)}", preset.colour is not None),
                )
                if present
            )
            embed.add_field(
                name=f"{preset.name}  ·  {total} slots",
                value=(
                    (f"{summary}\n{extras}" if extras else summary)[:990]
                    + f"\nby <@{preset.creator_id}>"
                ),
                inline=False,
            )
        embed.set_footer(
            text="/ava preset create · copy · edit · icon  ·  "
            "Right-click a raid post → Apps → Save as Raid Preset"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @preset.command(name="rename", description="Rename a raid preset.")
    @app_commands.describe(name="Preset to rename", new_name="Its new name")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_rename(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: app_commands.Range[str, 1, 50],
    ) -> None:
        preset = await self._get_owned_preset(interaction, name)
        if preset is None:
            return
        new_name = new_name.strip()
        if not new_name:
            await interaction.response.send_message("The new name can't be empty.", ephemeral=True)
            return
        if new_name == preset.name:
            await interaction.response.send_message(
                f"**{preset.name}** already has that name.", ephemeral=True
            )
            return
        clash = await self.db.get_preset(preset.guild_id, new_name)
        if clash is not None and clash.id != preset.id:
            await interaction.response.send_message(
                f"❌ A preset named **{clash.name}** already exists — pick another name.",
                ephemeral=True,
            )
            return
        await self.db.save_preset(
            guild_id=preset.guild_id,
            name=new_name,
            title=new_name,
            map_type=preset.map_type,
            roles=preset.roles,
            requirements=preset.requirements,
            creator_id=preset.creator_id,
            original_name=preset.name,
            image_path=preset.image_path,
            colour=preset.colour,
        )
        await interaction.response.send_message(
            f"✏️ Renamed **{preset.name}** → **{new_name}**.\n"
            f"Post it with `/ava create preset:{new_name}`",
            ephemeral=True,
        )

    @preset.command(name="delete", description="Delete raid presets (leave name empty to pick several).")
    @app_commands.describe(name="Preset to delete — leave empty to pick from a list")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_delete(self, interaction: discord.Interaction, name: str | None = None) -> None:
        if name:
            preset = await self._get_owned_preset(interaction, name)
            if preset is None:
                return
            await self._delete_presets([preset])
            await interaction.response.send_message(
                f"🗑️ Deleted preset **{preset.name}**.", ephemeral=True
            )
            return

        if not await self._require_member(interaction):
            return
        assert isinstance(interaction.user, discord.Member)
        host = is_event_host(interaction.user)
        presets = [
            p
            for p in await self.db.get_presets(interaction.guild_id or 0)
            if host or p.creator_id == interaction.user.id
        ]
        if not presets:
            await interaction.response.send_message(
                "You don't have any presets to delete — you can delete presets you made "
                "(Event Hosts can delete any).",
                ephemeral=True,
            )
            return
        view = PresetDeleteView(self, interaction.user.id, presets[:25])
        await interaction.response.send_message(
            "🗑️ **Pick the presets to delete**, then press **Delete**."
            + ("\n-# Showing the first 25." if len(presets) > 25 else ""),
            view=view,
            ephemeral=True,
        )

    async def _delete_presets(self, presets: list[RaidPreset]) -> list[str]:
        """Delete presets with their stored image and role icons. Returns deleted names."""
        deleted: list[str] = []
        for preset in presets:
            if await self.db.delete_preset(preset.guild_id, preset.name):
                delete_image(preset.image_path)
                await self._delete_icon_emojis(preset.id)
                deleted.append(preset.name)
        return deleted

    async def preset_role_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Roles of the preset chosen in the same command."""
        preset_name = getattr(interaction.namespace, "preset", None)
        if not preset_name or interaction.guild_id is None:
            return []
        preset = await self.db.get_preset(interaction.guild_id, preset_name)
        if preset is None:
            return []
        try:
            groups = parse_roles(preset.roles)
        except ValueError:
            return []
        current = current.lower()
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for _, name, _ in groups
            if current in name.lower()
        ][:25]

    async def _delete_icon_emojis(self, preset_id: int, only: str | None = None) -> None:
        """Remove role icons uploaded for a preset (or just the one named `only`)."""
        prefix = f"{ICON_EMOJI_PREFIX}{preset_id}_"
        try:
            emojis = await self.bot.fetch_application_emojis()
        except discord.HTTPException as exc:
            logger.warning("Couldn't list application emojis: %s", exc)
            return
        for emoji in emojis:
            if (only is None and emoji.name.startswith(prefix)) or emoji.name == only:
                try:
                    await emoji.delete()
                except discord.HTTPException as exc:
                    logger.warning("Couldn't delete role icon %s: %s", emoji.name, exc)

    @preset.command(name="icon", description="Set the picture shown in front of a role.")
    @app_commands.describe(
        preset="Preset to change",
        role="Role to give the picture",
        image="Picture to use (square works best; it's shrunk to emoji size)",
    )
    @app_commands.autocomplete(preset=preset_autocomplete, role=preset_role_autocomplete)
    async def preset_icon(
        self,
        interaction: discord.Interaction,
        preset: str,
        role: str,
        image: discord.Attachment,
    ) -> None:
        chosen = await self._get_owned_preset(interaction, preset)
        if chosen is None:
            return
        groups = parse_roles(chosen.roles)
        index = next(
            (i for i, (_, name, _) in enumerate(groups) if name.lower() == role.lower()),
            None,
        )
        if index is None:
            await interaction.response.send_message(
                f"**{chosen.name}** has no role called **{role}**.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        try:
            data = await icon_image_bytes(image)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        _, role_name, count = groups[index]
        emoji_name = icon_emoji_name(chosen.id, role_name)
        await self._delete_icon_emojis(chosen.id, only=emoji_name)  # replacing an old one
        try:
            emoji = await self.bot.create_application_emoji(name=emoji_name, image=data)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Discord rejected that image: {exc}", ephemeral=True)
            return

        groups[index] = (str(emoji), role_name, count)
        await self.db.save_preset(
            guild_id=chosen.guild_id,
            name=chosen.name,
            title=chosen.title,
            map_type=chosen.map_type,
            roles=format_roles(groups),
            requirements=chosen.requirements,
            creator_id=chosen.creator_id,
            image_path=chosen.image_path,
            colour=chosen.colour,
        )
        await interaction.followup.send(
            f"✅ {emoji} is now the icon for **{role_name}** in **{chosen.name}**.\n"
            "New raids from this preset will use it.",
            ephemeral=True,
        )

    @preset.command(name="clear-image", description="Go back to the default raid image.")
    @app_commands.describe(name="Preset to change")
    @app_commands.autocomplete(name=preset_autocomplete)
    async def preset_clear_image(self, interaction: discord.Interaction, name: str) -> None:
        chosen = await self._get_owned_preset(interaction, name)
        if chosen is None:
            return
        await self.db.save_preset(
            guild_id=chosen.guild_id,
            name=chosen.name,
            title=chosen.title,
            map_type=chosen.map_type,
            roles=chosen.roles,
            requirements=chosen.requirements,
            creator_id=chosen.creator_id,
            image_path=None,
            colour=chosen.colour,
        )
        delete_image(chosen.image_path)
        await interaction.response.send_message(
            f"🖼️ **{chosen.name}** now uses the default raid image.", ephemeral=True
        )

    # ── /ava ────────────────────────────────────────────────────────────

    @raid.command(name="create", description="Post an AVA raid signup from a preset.")
    @app_commands.describe(
        title="Name of the raid (shown at the top of the post)",
        preset="Which roster to use",
        time="Your local time with timezone — e.g. 10 PM EST, 9:30pm CST, 18 UTC",
        date="Optional — MM/DD, e.g. 11/29. Leave empty for today",
        ping="Ping the raid role set in /ava settings (default: yes)",
        description="Text shown on the post (default: the preset's description)",
    )
    @app_commands.autocomplete(preset=preset_autocomplete)
    async def raid_create(
        self,
        interaction: discord.Interaction,
        title: str,
        preset: str,
        time: str,
        date: str | None = None,
        ping: bool = True,
        description: app_commands.Range[str, 1, 1000] | None = None,
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
            start = combine_farm_datetime(
                date, time, default_tz=server_timezone(settings.timezone)
            )
            groups = parse_roles(chosen.roles)
        except ValueError as exc:
            await interaction.followup.send(
                f"❌ {exc}\n\n{time_help(settings.timezone)}", ephemeral=True
            )
            return

        slots = expand_roles(groups)
        final_title = title.strip()[:256]
        notes = (description if description is not None else chosen.requirements).strip()
        image = raid_image_for(chosen.image_path)
        image_name = image[1] if image else None
        ping_role_id = resolve_ping_role_id(settings.ping_role_id) if ping else None

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
            requirements=notes,
            closed=False,
            slots=[Slot(slot_number=n, role_name=r, emoji=e) for n, r, e in slots],
            colour=chosen.colour,
        )

        try:
            message = await channel.send(
                content=signup_ping_content(ping_role_id, guild_id),
                embed=build_farm_embed(preview, image_name=image_name),
                file=(
                    discord.File(image[0], filename=image[1])
                    if image
                    else discord.utils.MISSING
                ),
                allowed_mentions=signup_allowed_mentions(ping_role_id, guild_id),
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
            requirements=notes,
            slots=slots,
            colour=chosen.colour,
        )
        # Now that we have a raid ID, attach the dropdown + buttons. Re-read first: if the
        # start time has already passed, the auto-start may have closed it meanwhile.
        event = await self.db.get_event_by_id(event.id) or event
        await message.edit(
            embed=build_farm_embed(event, image_name=image_name), view=build_raid_view(event)
        )

        await interaction.followup.send(
            f"✅ Raid **#{event.id}** posted: {message.jump_url}\n"
            f"**Starts:** {discord_timestamp(start, 'F')} ({discord_timestamp(start, 'R')})\n"
            f"-# Read `{time}` as **{_read_as(start)}**. Wrong? "
            "`/ava edit time:` with a timezone, e.g. `10 PM CST`."
            + (
                "\n⚠️ That start time has already passed, so signups closed right away. "
                "Use `/ava edit` with a later time or a date to move it."
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
        date="New date — MM/DD, e.g. 11/29",
        time="New time with timezone — e.g. 10 PM EST, 9:30pm CST, 18 UTC",
        description="New text shown on the post (type - to remove it)",
    )
    @app_commands.autocomplete(raid=raid_autocomplete)
    async def raid_edit(
        self,
        interaction: discord.Interaction,
        raid: int | None = None,
        title: str | None = None,
        date: str | None = None,
        time: str | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return
        if not any([title, date, time, description]):
            await interaction.response.send_message(
                "Provide at least one field to change.", ephemeral=True
            )
            return

        event = await self._resolve_event(interaction, raid)
        if event is None:
            return

        start: datetime | None = None
        if date or time:
            settings = await self.db.get_guild_settings(event.guild_id)
            server_tz = server_timezone(settings.timezone)
            try:
                if time:
                    # No date given: keep the raid's current day (in the new timezone).
                    start = combine_farm_datetime(
                        date, time, default_date=event.start_time, default_tz=server_tz
                    )
                else:
                    year, month, day = parse_farm_date(date or "")
                    local = event.start_time.astimezone(server_tz)
                    start = local.replace(year=year, month=month, day=day)
            except ValueError as exc:
                await self._reply(interaction, f"❌ {exc}\n\n{time_help(settings.timezone)}")
                return

        await self.db.update_event_fields(
            event.id,
            title=title,
            start_time=start,
            requirements=(
                None if description is None
                else "" if description.strip() == "-"
                else description.strip()
            ),
        )
        if start is not None and start > datetime.now(timezone.utc):
            # Moved into the future: signups reopen and it will auto-close again then.
            await self.db.set_started(event.id, False)
            if event.closed and not event.loot_thread_id:
                await self.db.set_closed(event.id, False)
        refreshed = await self.db.get_event_by_id(event.id)
        if refreshed:
            await self._update_signup_message(refreshed)
        await self._reply(
            interaction,
            f"✏️ Updated raid **#{event.id}**."
            + (
                f"\n**Starts:** {discord_timestamp(start, 'F')} ({discord_timestamp(start, 'R')})"
                f"\n-# Read as **{_read_as(start)}**."
                if start is not None
                else ""
            ),
        )

    # ── /ava settings & /ava loot ────────────────────────────────────────

    # ── /overview ────────────────────────────────────────────────────────

    @app_commands.command(name="overview", description="See every upcoming raid with links to sign up.")
    @app_commands.describe(
        scope="Show raids from the whole server or just this channel",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Server", value="server"),
            app_commands.Choice(name="This channel", value="channel"),
        ]
    )
    @app_commands.guild_only()
    async def overview(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        assert guild is not None
        only_channel = scope is not None and scope.value == "channel"
        now = datetime.now(timezone.utc)

        events = [
            e
            for e in await self.db.get_events_by_guild(guild.id)
            if e.start_time >= now - OVERVIEW_IN_PROGRESS
            and e.message_id
            and (not only_channel or e.channel_id == interaction.channel_id)
        ]
        events.sort(key=lambda e: e.start_time)

        # Skip raids whose signup post was deleted — their links would go nowhere.
        await interaction.response.defer(thinking=True)
        exists = await asyncio.gather(*(self._post_exists(e) for e in events))
        events = [e for e, ok in zip(events, exists) if ok]

        where =f"#{getattr(interaction.channel, 'name', 'this channel')}" if only_channel else guild.name
        embed = discord.Embed(
            title=f"📅 Raid Overview — {where}"[:256], colour=config.EMBED_COLOUR_OPEN
        )
        if not events:
            embed.description = "No upcoming raids. Hosts can post one with `/ava create`."
        else:
            lines: list[str] = []
            shown = 0
            for event in events:
                link = (
                    f"https://discord.com/channels/{event.guild_id}/"
                    f"{event.channel_id}/{event.message_id}"
                )
                started = event.start_time <= now
                status = "🔴 in progress" if started else f"⏳ {discord_timestamp(event.start_time, 'R')}"
                full = event.total_slots and event.filled_slots >= event.total_slots
                line = (
                    f"**[{event.title[:80]}]({link})**\n"
                    f"🗓️ {discord_timestamp(event.start_time, 'f')} · {status}\n"
                    f"👥 **{event.filled_slots}/{event.total_slots}**"
                    + (" · FULL" if full else "")
                    + ("" if only_channel else f" · <#{event.channel_id}>")
                )
                if sum(len(x) + 2 for x in lines) + len(line) > 3900:
                    break
                lines.append(line)
                shown += 1
            embed.description = "\n\n".join(lines)
            if shown < len(events):
                embed.description += f"\n\n…and {len(events) - shown} more"
        embed.set_footer(
            text=f"{len(events)} raid(s) · times show in your own timezone · click a title to sign up"
        )
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _post_exists(self, event: FarmEvent) -> bool:
        """False only when Discord says the raid's signup message is gone."""
        channel = self.bot.get_channel(event.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return True  # channel not cached — can't check, keep it listed
        try:
            await channel.fetch_message(event.message_id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True  # can't tell (permissions, outage) — keep it listed

    @raid.command(
        name="settings", description="Set where raids post, where loot threads open and who gets pinged."
    )
    @app_commands.describe(
        raid_channel="Channel every /ava create posts into",
        loot_channel="Channel where a loot thread opens when each raid starts",
        ping_role="Role pinged when a new raid is posted",
        no_ping="Turn off the ping for new raids (pick a ping_role to turn it back on)",
        timezone="Timezone used when someone types a time like 10pm without one",
    )
    @app_commands.choices(
        timezone=[app_commands.Choice(name=label, value=value) for label, value in TIMEZONE_CHOICES]
    )
    async def ava_settings(
        self,
        interaction: discord.Interaction,
        raid_channel: discord.TextChannel | None = None,
        loot_channel: discord.TextChannel | None = None,
        ping_role: discord.Role | None = None,
        no_ping: bool = False,
        timezone: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._require_host(interaction):
            return
        guild_id = interaction.guild_id or 0
        if ping_role is not None and no_ping:
            await interaction.response.send_message(
                "Pick a `ping_role` **or** set `no_ping` — not both.", ephemeral=True
            )
            return
        warning = ""
        if ping_role is not None and not ping_role.is_default():
            me = interaction.guild.me if interaction.guild else None
            if not ping_role.mentionable and not (
                me and me.guild_permissions.mention_everyone
            ):
                warning = (
                    f"\n⚠️ {ping_role.mention} isn't mentionable and I don't have "
                    "**Mention @everyone, @here and All Roles** — the ping won't notify "
                    "anyone. Turn on *Allow anyone to @mention this role* in the role "
                    "settings, or give me that permission."
                )
        if any(x is not None for x in (raid_channel, loot_channel, ping_role, timezone)) or no_ping:
            await self.db.update_guild_settings(
                guild_id,
                raid_channel_id=raid_channel.id if raid_channel else None,
                loot_channel_id=loot_channel.id if loot_channel else None,
                ping_role_id=0 if no_ping else (ping_role.id if ping_role else None),
                timezone=timezone.value if timezone else None,
            )
        settings = await self.db.get_guild_settings(guild_id)

        def show(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "_not set_"

        ping_id = resolve_ping_role_id(settings.ping_role_id)
        ping_text = (
            "_off_" if ping_id is None
            else "@everyone" if ping_id == guild_id
            else f"<@&{ping_id}>"
        )
        await interaction.response.send_message(
            "⚙️ **AVA settings**\n"
            f"📣 Raid posts: {show(settings.raid_channel_id)}"
            + ("" if settings.raid_channel_id else " _(posts where you run /ava create)_")
            + f"\n🔔 Ping for new raids: {ping_text}"
            + f"\n🕒 Server timezone: **{timezone_label(settings.timezone)}** "
            "_(used when a time is typed without one, e.g. `10pm`)_"
            + f"\n💰 Loot threads: {show(settings.loot_channel_id)}\n\n"
            "When a raid's start time passes, signups close and its loot thread opens."
            + warning,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
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
