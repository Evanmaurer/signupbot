"""AVA balances — what each member has earned from raid payouts.

Confirmed loot-thread payouts are credited automatically; balance admins can add,
remove, reset one member, or wipe the whole server.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config
from split_calculator import format_silver

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

_AMOUNT = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmb])?$", re.I)
_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
MAX_AMOUNT = 1_000_000_000_000
HISTORY_LIMIT = 20


def _signed(amount: int) -> str:
    return f"+{format_silver(amount)}" if amount > 0 else f"−{format_silver(-amount)}"


def parse_amount(text: str) -> int:
    """Parse '1,500,000', '1.5m', '500k' or '2b' into silver."""
    cleaned = text.replace(",", "").replace(" ", "").strip()
    match = _AMOUNT.match(cleaned)
    if not match:
        raise ValueError(f'"{text}" isn\'t an amount — use e.g. `1,500,000`, `1.5m` or `500k`.')
    value = float(match.group(1)) * _MULTIPLIERS.get((match.group(2) or "").lower(), 1)
    amount = round(value)
    if amount <= 0:
        raise ValueError("The amount must be more than 0.")
    if amount > MAX_AMOUNT:
        raise ValueError("That amount is too large.")
    return amount


def is_balance_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return config.BALANCE_ADMIN_ROLE_ID is not None and any(
        role.id == config.BALANCE_ADMIN_ROLE_ID for role in member.roles
    )


class WipeConfirmView(discord.ui.View):
    """Confirm step for wiping every balance in the server."""

    def __init__(self, cog: BalanceCog, admin_id: int) -> None:
        super().__init__(timeout=60)
        self.cog, self.admin_id = cog, admin_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.admin_id

    @discord.ui.button(label="Wipe every balance", emoji="🧨", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        guild_id = interaction.guild_id or 0
        count = await self.cog.db.wipe_balances(guild_id)
        logger.warning(
            "AVA balances wiped in guild %s by %s (%d members)", guild_id, interaction.user.id, count
        )
        await interaction.response.edit_message(
            content=f"🧨 Wiped the AVA balances of **{count}** member(s). Everyone is at 0.",
            view=None,
        )
        if interaction.channel is not None and isinstance(interaction.channel, discord.abc.Messageable):
            try:
                await interaction.channel.send(
                    f"🧨 {interaction.user.mention} wiped all AVA balances in the server.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled — nothing wiped.", view=None)


class BalanceCog(commands.Cog):
    """AVA payout balances."""


    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member) and is_balance_admin(member):
            return True
        role = f"<@&{config.BALANCE_ADMIN_ROLE_ID}>" if config.BALANCE_ADMIN_ROLE_ID else "admin"
        await interaction.response.send_message(
            f"Only {role} can change balances.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    # ── everyone ─────────────────────────────────────────────────────────

    @app_commands.command(name="bal", description="Show an AVA balance — yours, or anyone's.")
    @app_commands.describe(member="Whose balance to show (leave empty for yours)")
    @app_commands.guild_only()
    async def bal_view(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild_id = interaction.guild_id or 0
        target = member or interaction.user

        balance = await self.db.get_balance(guild_id, target.id)
        embed = discord.Embed(
            title=f"💰 AVA balance — {target.display_name}"[:256],
            description=f"## {format_silver(balance)} silver",
            colour=config.EMBED_COLOUR_OPEN,
        )
        embed.set_footer(text="Raid payouts are added when the raid lead confirms them")
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(
        name="bal-history", description="See where an AVA balance came from — payouts and changes."
    )
    @app_commands.describe(member="Whose history to show (leave empty for yours)")
    @app_commands.guild_only()
    async def bal_history(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild_id = interaction.guild_id or 0
        target = member or interaction.user
        balance = await self.db.get_balance(guild_id, target.id)
        history = await self.db.get_balance_history(guild_id, target.id, limit=HISTORY_LIMIT)

        lines: list[str] = []
        for row in history:
            when = datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)
            if row["kind"] == "payout":
                title = row["title"] or f"raid #{row['event_id']}"
                what = f"AVA payout — {title}"
            else:
                sign = "Added" if row["amount"] > 0 else "Removed"
                by = f" by <@{row['created_by']}>" if row["created_by"] else ""
                what = f"{sign}{by}" + (f" — {row['reason']}" if row["reason"] else "")
            lines.append(f"`{_signed(row['amount'])}` {what} · <t:{int(when.timestamp())}:d>")

        body = ""
        for line in lines:
            if len(body) + len(line) + 1 > 3800:
                break
            body += line + "\n"
        embed = discord.Embed(
            title=f"📜 AVA balance history — {target.display_name}"[:256],
            description=(
                f"**Balance: {format_silver(balance)} silver**\n\n" + (body or "_Nothing yet._")
            ),
            colour=config.EMBED_COLOUR_OPEN,
        )
        embed.set_footer(text=f"Latest {HISTORY_LIMIT} changes, newest first")
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="bals", description="Everyone's AVA balance, highest first.")
    @app_commands.guild_only()
    async def bal_list(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0
        balances = await self.db.get_balances(guild_id)
        if not balances:
            await interaction.response.send_message("Nobody has an AVA balance yet.")
            return
        lines = [
            f"**{i}.** <@{uid}> — **{format_silver(amount)}**"
            for i, (uid, amount) in enumerate(balances, start=1)
        ]
        body, shown = "", 0
        for line in lines:
            if len(body) + len(line) + 1 > 3900:
                break
            body += line + "\n"
            shown += 1
        if shown < len(lines):
            body += f"…and {len(lines) - shown} more"
        embed = discord.Embed(
            title="💰 AVA balances", description=body, colour=config.EMBED_COLOUR_OPEN
        )
        embed.set_footer(
            text=f"{len(balances)} member(s) · total {format_silver(sum(a for _, a in balances))} silver"
        )
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # ── admins ───────────────────────────────────────────────────────────

    async def _adjust(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount_text: str,
        reason: str | None,
        sign: int,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        if sign < 0 and amount_text.strip().lower() == "all":
            # Take off everything they have.
            current = await self.db.get_balance(interaction.guild_id or 0, member.id)
            if current <= 0:
                await interaction.response.send_message(
                    f"{member.mention} has nothing to remove (balance {format_silver(current)}).",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            amount = -current
        else:
            try:
                amount = parse_amount(amount_text) * sign
            except ValueError as exc:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
                return
        new_balance = await self.db.add_balance(
            interaction.guild_id or 0,
            member.id,
            amount,
            reason=(reason or "").strip()[:200],
            created_by=interaction.user.id,
        )
        logger.info(
            "AVA balance %s for %s by %s: %s (%s)",
            "add" if sign > 0 else "remove", member.id, interaction.user.id, amount, reason,
        )
        verb = "Added" if sign > 0 else "Removed"
        prep = "to" if sign > 0 else "from"
        await interaction.response.send_message(
            f"✅ {verb} **{format_silver(abs(amount))}** {prep} {member.mention}"
            + (f" _({reason.strip()})_" if reason and reason.strip() else "")
            + f".\nNew balance: **{format_silver(new_balance)}**",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="bal-add", description="Add silver to a member's AVA balance (admins).")
    @app_commands.guild_only()
    @app_commands.describe(
        member="Who to give it to",
        amount="How much — e.g. 1,500,000, 1.5m or 500k",
        reason="Optional note shown in their history",
    )
    async def bal_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: str,
        reason: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        await self._adjust(interaction, member, amount, reason, +1)

    @app_commands.command(name="bal-remove", description="Take silver off a member's AVA balance, e.g. once paid (admins).")
    @app_commands.guild_only()
    @app_commands.describe(
        member="Whose balance to lower",
        amount="How much — e.g. 1,500,000, 1.5m, 500k, or all",
        reason="Optional note, e.g. 'paid out'",
    )
    async def bal_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: str,
        reason: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        await self._adjust(interaction, member, amount, reason, -1)

    @app_commands.command(name="bal-reset", description="Set one member's AVA balance back to 0 (admins).")
    @app_commands.guild_only()
    @app_commands.describe(member="Whose balance to clear")
    async def bal_reset(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._require_admin(interaction):
            return
        before = await self.db.reset_balance(interaction.guild_id or 0, member.id)
        logger.info("AVA balance reset for %s by %s (was %s)", member.id, interaction.user.id, before)
        await interaction.response.send_message(
            f"🧹 Reset {member.mention}'s AVA balance to **0** (was {format_silver(before)}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="bal-wipe", description="Wipe EVERY AVA balance in this server (admins).")
    @app_commands.guild_only()
    async def bal_wipe(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        balances = await self.db.get_balances(interaction.guild_id or 0)
        total = sum(a for _, a in balances)
        await interaction.response.send_message(
            f"⚠️ This sets **every** AVA balance in the server to 0 — **{len(balances)}** member(s), "
            f"**{format_silver(total)}** silver in total. It can't be undone.",
            view=WipeConfirmView(self, interaction.user.id),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BalanceCog(bot, bot.db))  # type: ignore[attr-defined]
