"""Discord Fame Farm Signup Bot entry point."""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys

import aiohttp
import certifi
import discord
from discord.ext import commands

import config
from cogs.farm import FarmCog
from cogs.reaction_roles import ReactionRolesCog
from cogs.split import SplitCog
from database import Database

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True


def create_ssl_connector() -> aiohttp.TCPConnector:
    """Use certifi's CA bundle (fixes macOS python.org SSL verify failures)."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_context)


class SignupBot(commands.Bot):
    """Bot subclass that owns the database connection."""

    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=INTENTS,
            connector=create_ssl_connector(),
        )
        self.db = Database(config.DATABASE_PATH)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.add_cog(FarmCog(self, self.db))
        await self.add_cog(SplitCog(self, self.db))
        await self.add_cog(ReactionRolesCog(self, self.db))
        await self.tree.sync()
        events = await self.db.get_active_events()
        logger.info("Restored %d active event(s) from database", len(events))

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def main() -> None:
    if not config.BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and configure it.")
        sys.exit(1)

    bot = SignupBot()
    async with bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutdown requested")
