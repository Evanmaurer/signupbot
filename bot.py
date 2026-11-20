"""Take Care AVA — Albion Online AVA raid signup & loot-split bot."""

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
from cogs.balance import BalanceCog
from cogs.loot import LootCog
from cogs.raid import RaidCog
from cogs.split import SplitCog
from database import Database
from role_icons import sync_role_icons

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
            enable_debug_events=True,
        )
        self.db = Database(config.DATABASE_PATH)
        self._gateway_events: dict[str, int] = {}

    async def setup_hook(self) -> None:
        await self.db.connect()
        await sync_role_icons(self)
        await self.add_cog(RaidCog(self, self.db))
        await self.add_cog(LootCog(self, self.db))
        await self.add_cog(SplitCog(self, self.db))
        await self.add_cog(BalanceCog(self, self.db))
        await self.tree.sync()
        events = await self.db.get_active_events()
        logger.info("Restored %d active event(s) from database", len(events))
        asyncio.create_task(self._connection_report())

    async def on_socket_event_type(self, event_type: str) -> None:
        self._gateway_events[event_type] = self._gateway_events.get(event_type, 0) + 1

    async def _connection_report(self) -> None:
        """Log connection health once a minute (helps spot a silently dead link)."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(60)
            logger.info(
                "Connection check: latency=%.0fms gateway events in last minute=%s",
                self.latency * 1000,
                dict(sorted(self._gateway_events.items())) or "none",
            )
            self._gateway_events.clear()

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
