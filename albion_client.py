"""Small Albion Online gameinfo API client."""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp
import certifi


REGION_BASE_URLS = {
    "americas": "https://gameinfo.albiononline.com/api/gameinfo",
    "america": "https://gameinfo.albiononline.com/api/gameinfo",
    "us": "https://gameinfo.albiononline.com/api/gameinfo",
    "europe": "https://gameinfo-ams.albiononline.com/api/gameinfo",
    "eu": "https://gameinfo-ams.albiononline.com/api/gameinfo",
    "asia": "https://gameinfo-sgp.albiononline.com/api/gameinfo",
    "sgp": "https://gameinfo-sgp.albiononline.com/api/gameinfo",
}


class AlbionAPIError(RuntimeError):
    """Raised when Albion gameinfo cannot return usable battle data."""


class AlbionBattleNotFound(AlbionAPIError):
    """Raised when a battle ID does not exist in the selected region."""


@dataclass(slots=True)
class AlbionBattlePlayer:
    """Player data needed for Discord role assignment."""

    id: str
    name: str
    guild_id: str
    guild_name: str


def albion_base_url(region: str) -> str:
    """Resolve a configured region to the Albion gameinfo base URL."""
    normalized = region.strip().lower()
    if normalized in REGION_BASE_URLS:
        return REGION_BASE_URLS[normalized]
    return REGION_BASE_URLS["americas"]


def create_albion_connector() -> aiohttp.TCPConnector:
    """Create a connector tuned for Albion gameinfo from local bot hosts."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(family=socket.AF_INET, ssl=ssl_context)


def parse_battle_players(data: Any, guild_id: str) -> list[AlbionBattlePlayer]:
    """Extract configured-guild players from an Albion battle response."""
    if not isinstance(data, dict):
        raise AlbionAPIError("Albion returned an invalid battle response.")

    players = data.get("players")
    if not isinstance(players, dict):
        raise AlbionAPIError("Albion battle response did not include players.")

    target_guild_id = guild_id.strip()
    battle_players: list[AlbionBattlePlayer] = []

    for raw_player in players.values():
        if not isinstance(raw_player, dict):
            continue
        player_guild_id = str(raw_player.get("guildId") or "").strip()
        if player_guild_id != target_guild_id:
            continue

        player_name = str(raw_player.get("name") or "").strip()
        player_id = str(raw_player.get("id") or "").strip()
        if not player_name or not player_id:
            continue

        battle_players.append(
            AlbionBattlePlayer(
                id=player_id,
                name=player_name,
                guild_id=player_guild_id,
                guild_name=str(raw_player.get("guildName") or "").strip(),
            )
        )

    return sorted(battle_players, key=lambda player: player.name.casefold())


async def fetch_battle_players(
    battle_id: str,
    guild_id: str,
    *,
    region: str,
    timeout_seconds: float = 60.0,
) -> list[AlbionBattlePlayer]:
    """Fetch a battle and return only players from the configured Albion guild."""
    base_url = albion_base_url(region)
    url = f"{base_url}/battles/{battle_id}"
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        connect=min(timeout_seconds, 20.0),
        sock_connect=min(timeout_seconds, 20.0),
        sock_read=timeout_seconds,
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "signupbot/1.0 Discord role assignment",
    }

    try:
        async with aiohttp.ClientSession(
            connector=create_albion_connector(),
            headers=headers,
            timeout=timeout,
        ) as session:
            async with session.get(url) as response:
                if response.status == 404:
                    raise AlbionBattleNotFound(
                        f"Albion battle `{battle_id}` was not found in `{region}`."
                    )
                if response.status >= 400:
                    raise AlbionAPIError(
                        f"Albion API returned HTTP {response.status} for battle `{battle_id}`."
                    )
                data = await response.json(content_type=None)
    except asyncio.TimeoutError as exc:
        raise AlbionAPIError(
            f"Timed out after {timeout_seconds:g}s while fetching the Albion battleboard."
        ) from exc
    except aiohttp.ClientError as exc:
        raise AlbionAPIError(f"Could not reach Albion API: {exc}") from exc
    except ValueError as exc:
        raise AlbionAPIError("Albion returned invalid JSON for that battle.") from exc

    return parse_battle_players(data, guild_id)
