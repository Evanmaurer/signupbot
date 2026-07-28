"""Assign Discord roles to guild members found in an Albion battleboard."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

import config
from albion_client import (
    AlbionAPIError,
    AlbionBattleNotFound,
    AlbionBattlePlayer,
    fetch_battle_players,
)
from name_match import member_name_keys, normalize_match_name
from role_menu import (
    RoleAssignOutcome,
    RoleAssignStatus,
    assign_role_to_members,
    build_bulk_role_embed,
    resolve_guild_role,
    validate_role_assignable,
)
from utils import can_manage_roles_staff

logger = logging.getLogger(__name__)

MAX_BATTLE_IDS = 10


@dataclass(slots=True)
class AlbionMemberMatch:
    """Result of matching Albion player names to Discord members."""

    members: list[discord.Member]
    failed_outcomes: list[RoleAssignOutcome]


def _member_name_candidates(member: discord.Member) -> set[str]:
    return member_name_keys(
        nick=getattr(member, "nick", None),
        display_name=getattr(member, "display_name", None),
        name=getattr(member, "name", None),
        global_name=getattr(member, "global_name", None),
    )


def match_albion_players_to_members(
    players: list[AlbionBattlePlayer],
    members: list[discord.Member],
) -> AlbionMemberMatch:
    """Match Albion player names against Discord member names."""
    member_index: dict[str, list[discord.Member]] = {}
    for member in members:
        if getattr(member, "bot", False):
            continue
        for candidate in _member_name_candidates(member):
            bucket = member_index.setdefault(candidate, [])
            if all(existing.id != member.id for existing in bucket):
                bucket.append(member)

    matched_members: list[discord.Member] = []
    failed: list[RoleAssignOutcome] = []
    seen_member_ids: set[int] = set()

    for player in players:
        matches = member_index.get(normalize_match_name(player.name), [])
        if not matches:
            failed.append(
                RoleAssignOutcome(
                    display_name=player.name,
                    user_id=0,
                    status=RoleAssignStatus.FAILED,
                    reason="not found in Discord",
                )
            )
            continue

        if len(matches) > 1:
            failed.append(
                RoleAssignOutcome(
                    display_name=player.name,
                    user_id=0,
                    status=RoleAssignStatus.FAILED,
                    reason="ambiguous Discord match",
                )
            )
            continue

        member = matches[0]
        if member.id in seen_member_ids:
            failed.append(
                RoleAssignOutcome(
                    display_name=player.name,
                    user_id=member.id,
                    status=RoleAssignStatus.SKIPPED,
                    reason="duplicate Discord match",
                )
            )
            continue

        seen_member_ids.add(member.id)
        matched_members.append(member)

    return AlbionMemberMatch(members=matched_members, failed_outcomes=failed)


class AlbionRolesCog(commands.Cog):
    """Commands for Albion battleboard based role assignment."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    async def _send_denied(
        *,
        interaction: discord.Interaction | None = None,
        ctx: commands.Context | None = None,
        message: str,
    ) -> None:
        if interaction is not None:
            await interaction.response.send_message(message, ephemeral=True)
        elif ctx is not None:
            await ctx.reply(message)

    async def _require_staff(
        self,
        *,
        interaction: discord.Interaction | None = None,
        ctx: commands.Context | None = None,
    ) -> discord.Member | None:
        user: discord.abc.User | None = None
        if interaction is not None:
            user = interaction.user
        elif ctx is not None:
            user = ctx.author

        if not isinstance(user, discord.Member):
            await self._send_denied(
                interaction=interaction,
                ctx=ctx,
                message="This command can only be used in a server.",
            )
            return None

        if not can_manage_roles_staff(user):
            await self._send_denied(
                interaction=interaction,
                ctx=ctx,
                message=(
                    "You need **Administrator**, **Manage Roles**, "
                    "or the Event Host role."
                ),
            )
            return None
        return user

    @staticmethod
    def _parse_battle_ids(battle_ids: str) -> list[str]:
        cleaned = battle_ids.strip()
        if not cleaned:
            raise ValueError("Enter at least one Albion battle ID.")

        ids: list[str] = []
        seen: set[str] = set()
        for token in re.split(r"[\s,]+", cleaned):
            if not token:
                continue
            if not token.isdigit():
                raise ValueError(
                    "Battle IDs must be numeric. Separate multiple IDs with spaces or commas."
                )
            if token not in seen:
                seen.add(token)
                ids.append(token)

        if not ids:
            raise ValueError("Enter at least one Albion battle ID.")
        if len(ids) > MAX_BATTLE_IDS:
            raise ValueError(f"Use at most {MAX_BATTLE_IDS} battle IDs at once.")
        return ids

    @staticmethod
    def _parse_prefix_args(
        guild: discord.Guild,
        args: str,
        role_mentions: list[discord.Role],
    ) -> tuple[list[str], discord.Role]:
        if role_mentions:
            id_text = args
            for role in role_mentions:
                id_text = re.sub(fr"<@&{role.id}>", " ", id_text)
            return AlbionRolesCog._parse_battle_ids(id_text), role_mentions[0]

        match = re.match(r"^\s*((?:\d+[\s,]+)*\d+)\s+(.+?)\s*$", args)
        if match is None:
            raise ValueError("Usage: `!bbrole <battle_id> [battle_id ...] @Role`")
        battle_ids = AlbionRolesCog._parse_battle_ids(match.group(1))
        role_text = match.group(2).strip()
        return AlbionRolesCog._parse_battle_ids(" ".join(battle_ids)), resolve_guild_role(
            guild, role_text
        )

    @staticmethod
    async def _load_members(guild: discord.Guild) -> list[discord.Member]:
        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except discord.HTTPException as exc:
                logger.warning("Could not chunk guild %s before bbrole matching: %s", guild.id, exc)

        members = [member for member in guild.members if not member.bot]
        if members:
            return members

        fetched: list[discord.Member] = []
        try:
            async for member in guild.fetch_members(limit=None):
                if not member.bot:
                    fetched.append(member)
        except discord.HTTPException as exc:
            logger.warning("Could not fetch guild members for guild %s: %s", guild.id, exc)
        return fetched

    async def _build_assignment_embed(
        self,
        *,
        guild: discord.Guild,
        moderator: discord.Member,
        battle_ids: list[str],
        role: discord.Role,
    ) -> discord.Embed | str:
        if not config.ALBION_GUILD_ID:
            return (
                "Set `ALBION_GUILD_ID` in `.env` before using this command. "
                "Use the Albion guild ID, not the Discord server ID."
            )

        validate_role_assignable(guild, role)

        players_by_id: dict[str, AlbionBattlePlayer] = {}
        player_counts: list[str] = []
        for battle_id in battle_ids:
            try:
                battle_players = await fetch_battle_players(
                    battle_id,
                    config.ALBION_GUILD_ID,
                    region=config.ALBION_REGION,
                    timeout_seconds=config.ALBION_TIMEOUT_SECONDS,
                )
            except AlbionBattleNotFound as exc:
                return str(exc)
            except AlbionAPIError as exc:
                return str(exc)

            player_counts.append(f"`{battle_id}`: {len(battle_players)}")
            for player in battle_players:
                players_by_id[player.id] = player

        players = sorted(players_by_id.values(), key=lambda player: player.name.casefold())
        battle_label = ", ".join(battle_ids)

        if not players:
            return (
                f"No players from configured Albion guild `{config.ALBION_GUILD_ID}` "
                f"were found in battle(s) `{battle_label}`."
            )

        members = await self._load_members(guild)
        matches = match_albion_players_to_members(players, members)

        outcomes = await assign_role_to_members(
            matches.members,
            role,
            reason=f"Albion battleboard {battle_label} assign by {moderator} ({moderator.id})",
        )
        outcomes.extend(matches.failed_outcomes)

        logger.info(
            "Albion bbrole by %s (%s): battles=%s role=%s (%s) "
            "albion_players=%d matched=%d unresolved=%d",
            moderator,
            moderator.id,
            battle_ids,
            role.name,
            role.id,
            len(players),
            len(matches.members),
            len(matches.failed_outcomes),
        )

        embed = build_bulk_role_embed(role, outcomes, moderator=moderator)
        embed.description = (
            f"Albion battle(s): `{battle_label}`\n"
            f"{len(players)} unique configured-guild player(s)\n"
            f"Per battle: {' | '.join(player_counts)}"
        )
        return embed

    @app_commands.command(
        name="bbrole",
        description="Assign a role to your guild members found in an Albion battleboard.",
    )
    @app_commands.describe(
        battle_id="One or more Albion battleboard IDs, separated by spaces or commas",
        role="The Discord role to assign",
    )
    async def bbrole_slash(
        self,
        interaction: discord.Interaction,
        battle_id: str,
        role: discord.Role,
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
            battle_ids = self._parse_battle_ids(battle_id)
            result = await self._build_assignment_embed(
                guild=interaction.guild,
                moderator=moderator,
                battle_ids=battle_ids,
                role=role,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if isinstance(result, discord.Embed):
            await interaction.followup.send(embed=result)
        else:
            await interaction.followup.send(result, ephemeral=True)

    @commands.command(name="bbrole")
    @commands.guild_only()
    async def bbrole_prefix(
        self,
        ctx: commands.Context,
        *,
        args: str = "",
    ) -> None:
        """Assign a role to guild members found in an Albion battleboard.

        Usage: !bbrole 123456789 987654321 @Role
        """
        moderator = await self._require_staff(ctx=ctx)
        if moderator is None:
            return

        assert ctx.guild is not None

        if not args.strip():
            await ctx.reply("Usage: `!bbrole <battle_id> [battle_id ...] @Role`")
            return

        async with ctx.typing():
            try:
                battle_ids, role = self._parse_prefix_args(
                    ctx.guild,
                    args,
                    ctx.message.role_mentions,
                )
                result = await self._build_assignment_embed(
                    guild=ctx.guild,
                    moderator=moderator,
                    battle_ids=battle_ids,
                    role=role,
                )
            except ValueError as exc:
                await ctx.reply(str(exc))
                return

        if isinstance(result, discord.Embed):
            await ctx.reply(embed=result)
        else:
            await ctx.reply(result)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AlbionRolesCog(bot))
