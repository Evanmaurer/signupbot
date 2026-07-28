"""Siphoned energy balance commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from name_match import member_name_keys
from siphon_energy import normalize_player_name, parse_siphon_export
from utils import can_manage_roles_staff

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

BALANCE_PAGE_SIZE = 20


def _member_name_candidates(member: discord.Member) -> set[str]:
    return member_name_keys(
        nick=getattr(member, "nick", None),
        display_name=getattr(member, "display_name", None),
        name=getattr(member, "name", None),
        global_name=getattr(member, "global_name", None),
    )


class SiphonCog(commands.Cog):
    """Manage siphoned energy balances from exported transaction files."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def _require_staff(
        self,
        *,
        interaction: discord.Interaction | None = None,
        ctx: commands.Context | None = None,
    ) -> discord.Member | None:
        user = interaction.user if interaction is not None else ctx.author if ctx else None
        if not isinstance(user, discord.Member):
            message = "This command can only be used in a server."
            if interaction is not None:
                await interaction.response.send_message(message, ephemeral=True)
            elif ctx is not None:
                await ctx.reply(message)
            return None

        if not can_manage_roles_staff(user):
            message = "You need **Administrator**, **Manage Roles**, or the Event Host role."
            if interaction is not None:
                await interaction.response.send_message(message, ephemeral=True)
            elif ctx is not None:
                await ctx.reply(message)
            return None
        return user

    async def _require_admin(
        self,
        ctx: commands.Context,
    ) -> discord.Member | None:
        if not isinstance(ctx.author, discord.Member):
            await ctx.reply("This command can only be used in a server.")
            return None
        if not ctx.author.guild_permissions.administrator:
            await ctx.reply("You need Discord Administrator permission.")
            return None
        return ctx.author

    @staticmethod
    async def _load_members(guild: discord.Guild) -> list[discord.Member]:
        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except discord.HTTPException as exc:
                logger.warning("Could not chunk guild %s for siphon sync: %s", guild.id, exc)

        members = [member for member in guild.members if not member.bot]
        if members:
            return members

        fetched: list[discord.Member] = []
        try:
            async for member in guild.fetch_members(limit=None):
                if not member.bot:
                    fetched.append(member)
        except discord.HTTPException as exc:
            logger.warning("Could not fetch guild members for siphon sync in %s: %s", guild.id, exc)
        return fetched

    @staticmethod
    def _match_players(
        balances: dict[str, int],
        display_names: dict[str, str],
        members: list[discord.Member],
    ) -> tuple[list[tuple[str, str, int | None, int]], list[str], list[str]]:
        member_index: dict[str, list[discord.Member]] = {}
        for member in members:
            for candidate in _member_name_candidates(member):
                bucket = member_index.setdefault(candidate, [])
                if all(existing.id != member.id for existing in bucket):
                    bucket.append(member)

        rows: list[tuple[str, str, int | None, int]] = []
        unmatched: list[str] = []
        ambiguous: list[str] = []
        for normalized, balance in sorted(
            balances.items(), key=lambda item: display_names[item[0]].casefold()
        ):
            player_name = display_names[normalized]
            matches = member_index.get(normalized, [])
            discord_user_id: int | None = None
            if len(matches) == 1:
                discord_user_id = matches[0].id
            elif len(matches) > 1:
                ambiguous.append(player_name)
            else:
                unmatched.append(player_name)
            rows.append((normalized, player_name, discord_user_id, balance))

        return rows, unmatched, ambiguous

    async def _read_attachment_text(self, attachment: discord.Attachment) -> str:
        data = await attachment.read()
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("Could not read that file as text.")

    async def _sync_from_text(
        self,
        *,
        guild: discord.Guild,
        moderator: discord.Member,
        text: str,
    ) -> discord.Embed:
        parsed = parse_siphon_export(text)
        members = await self._load_members(guild)
        rows, unmatched, ambiguous = self._match_players(
            parsed.balances,
            parsed.display_names,
            members,
        )
        await self.db.replace_siphon_balances(guild.id, rows)

        matched = sum(1 for _, _, user_id, _ in rows if user_id is not None)
        debtors = sum(1 for _, _, _, balance in rows if balance < 0)
        total_debt = sum(-balance for _, _, _, balance in rows if balance < 0)
        creditors = sum(1 for _, _, _, balance in rows if balance > 0)
        total_credit = sum(balance for _, _, _, balance in rows if balance > 0)
        top_debt = [
            f"**{player_name}**: {-balance}"
            for _, player_name, _, balance in sorted(rows, key=lambda row: row[3])[:10]
            if balance < 0
        ]
        top_credit = [
            f"**{player_name}**: +{balance}"
            for _, player_name, _, balance in sorted(rows, key=lambda row: row[3], reverse=True)[:10]
            if balance > 0
        ]

        embed = discord.Embed(
            title="Siphoned Energy Synced",
            colour=0x57F287,
        )
        embed.add_field(name="Rows Imported", value=str(parsed.rows), inline=True)
        if parsed.duplicate_rows:
            embed.add_field(
                name="Duplicate Rows Ignored",
                value=str(parsed.duplicate_rows),
                inline=True,
            )
        if parsed.ignored_rows:
            embed.add_field(
                name="Exempt Removes Ignored",
                value=str(parsed.ignored_rows),
                inline=True,
            )
        embed.add_field(name="Players", value=str(len(rows)), inline=True)
        embed.add_field(name="Matched Discord Users", value=str(matched), inline=True)
        embed.add_field(name="Players Needing Deposit", value=str(debtors), inline=True)
        embed.add_field(name="Total Needed", value=str(total_debt), inline=True)
        embed.add_field(name="Players With Credit", value=str(creditors), inline=True)
        embed.add_field(name="Total Credit", value=str(total_credit), inline=True)

        if top_debt:
            embed.add_field(name="Largest Needed Deposits", value="\n".join(top_debt), inline=False)
        if top_credit:
            embed.add_field(name="Largest Positive Balances", value="\n".join(top_credit), inline=False)
        if unmatched:
            sample = ", ".join(unmatched[:20])
            if len(unmatched) > 20:
                sample += f" ... +{len(unmatched) - 20} more"
            embed.add_field(name="Unmatched", value=sample, inline=False)
        if ambiguous:
            sample = ", ".join(ambiguous[:20])
            if len(ambiguous) > 20:
                sample += f" ... +{len(ambiguous) - 20} more"
            embed.add_field(name="Ambiguous Matches", value=sample, inline=False)

        embed.set_footer(text=f"By {moderator}")
        return embed

    async def _build_balances_embeds(
        self,
        *,
        guild_id: int,
        requester: discord.Member,
        view: str,
    ) -> list[discord.Embed]:
        normalized_view = view.strip().lower() if view else "all"
        if normalized_view not in {"all", "negative", "positive"}:
            raise ValueError("View must be `all`, `negative`, or `positive`.")

        balances = await self.db.get_siphon_balances(guild_id)
        if normalized_view == "negative":
            balances = [row for row in balances if row[3] < 0]
        elif normalized_view == "positive":
            balances = [row for row in balances if row[3] > 0]

        if normalized_view == "positive":
            balances = sorted(balances, key=lambda row: (-row[3], row[1].casefold()))
        else:
            balances = sorted(balances, key=lambda row: (row[3], row[1].casefold()))

        if not balances:
            embed = discord.Embed(
                title="Siphoned Energy Balances",
                description="No balances found. Run `!siphonsync` with an export file first.",
                colour=0xFEE75C,
            )
            embed.set_footer(text=f"By {requester}")
            return [embed]

        pages = [
            balances[index : index + BALANCE_PAGE_SIZE]
            for index in range(0, len(balances), BALANCE_PAGE_SIZE)
        ]

        embeds: list[discord.Embed] = []
        for page_number, page in enumerate(pages, start=1):
            lines: list[str] = []
            for _, player_name, discord_user_id, balance, _ in page:
                if balance < 0:
                    label = f"needs {-balance}"
                elif balance > 0:
                    label = f"credit +{balance}"
                else:
                    label = "even"
                mention = f" <@{discord_user_id}>" if discord_user_id else ""
                lines.append(f"**{player_name}**{mention} - {label}")

            title = "Siphoned Energy Balances"
            if normalized_view == "negative":
                title = "Siphoned Energy - Needs Deposit"
            elif normalized_view == "positive":
                title = "Siphoned Energy - Positive Balances"

            embed = discord.Embed(
                title=title,
                description="\n".join(lines),
                colour=0x5865F2,
            )
            embed.set_footer(
                text=f"By {requester} - Page {page_number}/{len(pages)}"
            )
            embeds.append(embed)

        return embeds

    @staticmethod
    def _format_single_balance(player_name: str, balance: int) -> str:
        if balance < 0:
            return f"**{player_name}** needs to deposit **{-balance}** siphoned energy."
        if balance > 0:
            return f"**{player_name}** has **+{balance}** siphoned energy credit."
        return f"**{player_name}** is even at **0** siphoned energy."

    async def _find_member_balance(
        self,
        *,
        guild_id: int,
        member: discord.Member,
    ) -> tuple[str, str, int | None, int, str] | None:
        balances = await self.db.get_siphon_balances(guild_id)
        for row in balances:
            if row[2] == member.id:
                return row

        candidates = _member_name_candidates(member)
        for row in balances:
            if row[0] in candidates:
                return row
        return None

    async def _set_member_balance(
        self,
        *,
        guild_id: int,
        member: discord.Member,
        balance: int,
    ) -> tuple[str, int]:
        existing = await self._find_member_balance(guild_id=guild_id, member=member)
        if existing is not None:
            normalized_player, player_name, _, _, _ = existing
        else:
            player_name = member.nick or member.display_name or member.name
            normalized_player = normalize_player_name(player_name)

        await self.db.set_siphon_balance(
            guild_id=guild_id,
            normalized_player=normalized_player,
            player_name=player_name,
            discord_user_id=member.id,
            balance=balance,
        )
        return player_name, balance

    async def _personal_balance_message(
        self,
        *,
        guild_id: int,
        member: discord.Member,
    ) -> str:
        balance_row = await self._find_member_balance(
            guild_id=guild_id,
            member=member,
        )
        if balance_row is None:
            return "I do not have a siphoned energy balance for you yet."

        _, player_name, _, balance, updated_at = balance_row
        return (
            f"{member.mention} {self._format_single_balance(player_name, balance)}\n"
            f"Last synced: `{updated_at}`"
        )

    @app_commands.command(
        name="siphonsync",
        description="Sync siphoned energy balances from a Date/Player/Reason/Amount export.",
    )
    @app_commands.describe(file="Tab-separated siphoned energy export file")
    async def siphonsync_slash(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        moderator = await self._require_staff(interaction=interaction)
        if moderator is None:
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        await interaction.response.defer()
        try:
            text = await self._read_attachment_text(file)
            embed = await self._sync_from_text(
                guild=interaction.guild,
                moderator=moderator,
                text=text,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="siphonbalances",
        description="Show siphoned energy positive and negative balances.",
    )
    @app_commands.describe(view="all, negative, or positive")
    async def siphonbalances_slash(
        self,
        interaction: discord.Interaction,
        view: str = "all",
    ) -> None:
        requester = await self._require_staff(interaction=interaction)
        if requester is None:
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        await interaction.response.defer()
        try:
            embeds = await self._build_balances_embeds(
                guild_id=interaction.guild.id,
                requester=requester,
                view=view,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await interaction.followup.send(embeds=embeds[:10])
        for index in range(10, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[index : index + 10])

    @app_commands.command(
        name="siphonebal",
        description="Show your siphoned energy balance.",
    )
    async def siphonebal_slash(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        message = await self._personal_balance_message(
            guild_id=interaction.guild.id,
            member=interaction.user,
        )
        await interaction.response.send_message(message, ephemeral=True)

    @commands.command(name="siphonsync")
    @commands.guild_only()
    async def siphonsync_prefix(self, ctx: commands.Context) -> None:
        """Sync siphoned energy balances from an attached TSV export."""
        moderator = await self._require_staff(ctx=ctx)
        if moderator is None:
            return
        if not ctx.message.attachments:
            await ctx.reply("Attach the siphoned energy export file, then run `!siphonsync`.")
            return

        assert ctx.guild is not None

        async with ctx.typing():
            try:
                text = await self._read_attachment_text(ctx.message.attachments[0])
                embed = await self._sync_from_text(
                    guild=ctx.guild,
                    moderator=moderator,
                    text=text,
                )
            except ValueError as exc:
                await ctx.reply(str(exc))
                return

        await ctx.reply(embed=embed)

    @commands.command(name="siphonbalances", aliases=["siphonbal", "siphons"])
    @commands.guild_only()
    async def siphonbalances_prefix(self, ctx: commands.Context, view: str = "all") -> None:
        """Show siphoned energy balances.

        Usage: !siphonbalances [all|negative|positive]
        """
        requester = await self._require_staff(ctx=ctx)
        if requester is None:
            return

        assert ctx.guild is not None

        async with ctx.typing():
            try:
                embeds = await self._build_balances_embeds(
                    guild_id=ctx.guild.id,
                    requester=requester,
                    view=view,
                )
            except ValueError as exc:
                await ctx.reply(str(exc))
                return

        await ctx.reply(embed=embeds[0])
        for embed in embeds[1:]:
            await ctx.send(embed=embed)

    @commands.command(
        name="siphonebal",
        aliases=[
            "siphonbalance",
            "siphonme",
            "mybal",
            "mysiphonebal",
            "mysiphonbal",
        ],
    )
    @commands.guild_only()
    async def siphonebal_prefix(self, ctx: commands.Context) -> None:
        """Show your siphoned energy balance.

        Usage: !siphonebal
        """
        if not isinstance(ctx.author, discord.Member):
            await ctx.reply("This command can only be used in a server.")
            return

        assert ctx.guild is not None

        message = await self._personal_balance_message(
            guild_id=ctx.guild.id,
            member=ctx.author,
        )
        await ctx.reply(message)

    @commands.command(name="editsiphonbal", aliases=["editsiphonebal"])
    @commands.guild_only()
    async def editsiphonbal_prefix(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        balance: int | None = None,
    ) -> None:
        """Set a member's siphoned energy balance.

        Usage: !editsiphonbal @User -50
        """
        admin = await self._require_admin(ctx)
        if admin is None:
            return

        assert ctx.guild is not None

        if member is None or balance is None:
            await ctx.reply("Usage: `!editsiphonbal @User <balance>`")
            return

        player_name, updated_balance = await self._set_member_balance(
            guild_id=ctx.guild.id,
            member=member,
            balance=balance,
        )
        logger.info(
            "Siphon balance edited in guild %s by %s (%s): user=%s balance=%s",
            ctx.guild.id,
            admin,
            admin.id,
            member.id,
            updated_balance,
        )
        await ctx.reply(
            f"Updated {member.mention}: "
            f"{self._format_single_balance(player_name, updated_balance)}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SiphonCog(bot, bot.db))  # type: ignore[attr-defined]
