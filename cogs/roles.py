"""Bulk role assignment and removal commands for staff."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from role_menu import (
    RoleAssignOutcome,
    RoleAssignStatus,
    assign_role_to_members,
    build_bulk_role_embed,
    parse_user_mentions,
    remove_role_from_members,
    resolve_members_from_ids,
    validate_role_assignable,
)
from utils import can_manage_roles_staff

logger = logging.getLogger(__name__)


class RolesCog(commands.Cog):
    """Multi-user role assignment and removal for moderators."""

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

    def _collect_mentioned_members(
        self, ctx: commands.Context
    ) -> list[discord.Member]:
        assert ctx.guild is not None
        members: list[discord.Member] = []
        for user in ctx.message.mentions:
            if self.bot.user and user.id == self.bot.user.id:
                continue
            member = (
                user
                if isinstance(user, discord.Member)
                else ctx.guild.get_member(user.id)
            )
            if isinstance(member, discord.Member):
                members.append(member)
        return members

    async def _run_bulk_change(
        self,
        *,
        guild: discord.Guild,
        moderator: discord.Member,
        role: discord.Role,
        members: list[discord.Member],
        missing_ids: list[int],
        action: str,
    ) -> discord.Embed:
        validate_role_assignable(guild, role)

        if action == "remove":
            outcomes = await remove_role_from_members(
                members,
                role,
                reason=f"Bulk remove by {moderator} ({moderator.id})",
            )
            success_status = RoleAssignStatus.REMOVED
            log_verb = "remove"
        else:
            outcomes = await assign_role_to_members(
                members,
                role,
                reason=f"Bulk assign by {moderator} ({moderator.id})",
            )
            success_status = RoleAssignStatus.ASSIGNED
            log_verb = "assign"

        for user_id in missing_ids:
            outcomes.append(
                RoleAssignOutcome(
                    display_name=f"Unknown user ({user_id})",
                    user_id=user_id,
                    status=RoleAssignStatus.FAILED,
                    reason="unknown user",
                )
            )

        success = [o for o in outcomes if o.status == success_status]
        failed = [o for o in outcomes if o.status == RoleAssignStatus.FAILED]

        logger.info(
            "Bulk role %s by %s (%s): role=%s (%s) success=%d failed=%d "
            "total=%d users=%s",
            log_verb,
            moderator,
            moderator.id,
            role.name,
            role.id,
            len(success),
            len(failed),
            len(outcomes),
            [o.user_id for o in outcomes],
        )

        return build_bulk_role_embed(
            role, outcomes, moderator=moderator, action=action
        )

    async def _slash_bulk(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        users: str,
        *,
        action: str,
    ) -> None:
        moderator = await self._require_staff(interaction=interaction)
        if moderator is None:
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        user_ids = parse_user_mentions(users)
        if not user_ids:
            verb = "remove from" if action == "remove" else "assign to"
            cmd = "removerole" if action == "remove" else "role"
            await interaction.response.send_message(
                f"Mention at least one user in the **users** field.\n"
                f"Example: `/{cmd} role:@Member users:@User1 @User2`",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            members, missing = await resolve_members_from_ids(
                interaction.guild, user_ids
            )
            embed = await self._run_bulk_change(
                guild=interaction.guild,
                moderator=moderator,
                role=role,
                members=members,
                missing_ids=missing,
                action=action,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await interaction.followup.send(embed=embed)

    async def _prefix_bulk(
        self,
        ctx: commands.Context,
        role: discord.Role | None,
        members: str,
        *,
        action: str,
    ) -> None:
        moderator = await self._require_staff(ctx=ctx)
        if moderator is None:
            return

        assert ctx.guild is not None

        cmd = "removerole" if action == "remove" else "role"
        if role is None:
            await ctx.reply(
                f"Usage: `!{cmd} @Role @User1 @User2 @User3`\n"
                "Mention a role first, then one or more users."
            )
            return

        mentioned_members = self._collect_mentioned_members(ctx)
        missing: list[int] = []
        if not mentioned_members:
            user_ids = parse_user_mentions(members)
            if not user_ids:
                await ctx.reply("Mention at least one user after the role.")
                return
            mentioned_members, missing = await resolve_members_from_ids(
                ctx.guild, user_ids
            )

        if not mentioned_members and not missing:
            await ctx.reply("Mention at least one user after the role.")
            return

        async with ctx.typing():
            try:
                embed = await self._run_bulk_change(
                    guild=ctx.guild,
                    moderator=moderator,
                    role=role,
                    members=mentioned_members,
                    missing_ids=missing,
                    action=action,
                )
            except ValueError as exc:
                await ctx.reply(str(exc))
                return

        await ctx.reply(embed=embed)

    @app_commands.command(
        name="role",
        description="Assign a role to one or more members at once.",
    )
    @app_commands.describe(
        role="The role to assign",
        users="Mention one or more users (e.g. @User1 @User2 @User3)",
    )
    async def role_slash(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        users: str,
    ) -> None:
        await self._slash_bulk(interaction, role, users, action="assign")

    @app_commands.command(
        name="removerole",
        description="Remove a role from one or more members at once.",
    )
    @app_commands.describe(
        role="The role to remove",
        users="Mention one or more users (e.g. @User1 @User2 @User3)",
    )
    async def removerole_slash(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        users: str,
    ) -> None:
        await self._slash_bulk(interaction, role, users, action="remove")

    @commands.command(name="role", aliases=["giverole"])
    @commands.guild_only()
    async def role_prefix(
        self,
        ctx: commands.Context,
        role: discord.Role | None = None,
        *,
        members: str = "",
    ) -> None:
        """Assign a role to multiple members.

        Usage: !role @Role @User1 @User2 @User3
        """
        await self._prefix_bulk(ctx, role, members, action="assign")

    @commands.command(name="removerole", aliases=["takerole"])
    @commands.guild_only()
    async def removerole_prefix(
        self,
        ctx: commands.Context,
        role: discord.Role | None = None,
        *,
        members: str = "",
    ) -> None:
        """Remove a role from multiple members.

        Usage: !removerole @Role @User1 @User2 @User3
        """
        await self._prefix_bulk(ctx, role, members, action="remove")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesCog(bot))
