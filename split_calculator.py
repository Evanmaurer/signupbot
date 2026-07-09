"""AVA loot split calculation logic and embed rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import discord

import config
from models import FarmEvent

_SILVER_INPUT_PATTERN = re.compile(r"^[\d,\s]+$")


class SellOffLocation(str, Enum):
    """Where loot was sold off."""

    CITY = "City"
    HIDEOUT = "Hideout"

    @classmethod
    def from_choice(cls, value: str) -> SellOffLocation:
        normalized = value.strip().lower()
        if normalized in {"city", "c"}:
            return cls.CITY
        if normalized in {"hideout", "h"}:
            return cls.HIDEOUT
        raise ValueError(f'Invalid sell-off location "{value}". Choose City or Hideout.')


@dataclass(slots=True)
class AvaSplitResult:
    """Full breakdown of an AVA loot split calculation."""

    event_id: int
    event_title: str
    total_item_value: int
    total_repairs: int
    item_pool: int
    sell_off_location: SellOffLocation
    sell_off_deduction_percent: float
    item_after_sell_off: int
    total_silver_bags: int
    map_cost: int
    silver_pool: int
    total_pool: int
    player_count: int
    per_player: int


def format_silver(amount: int | float) -> str:
    """Format a silver amount with thousands separators."""
    return f"{round(amount):,}"


def parse_silver_value(label: str, text: str) -> int:
    """Parse user silver input, allowing comma separators."""
    cleaned = text.replace(",", "").replace(" ", "").strip()
    if not cleaned or not _SILVER_INPUT_PATTERN.match(text.strip()):
        raise ValueError(f'Invalid {label}: "{text}". Enter a whole number (e.g. 18,500,000).')
    value = int(cleaned)
    if value < 0:
        raise ValueError(f"{label} cannot be negative.")
    return value


def sell_off_multiplier(location: SellOffLocation) -> tuple[float, float]:
    """Return (multiplier after deduction, deduction percent) for a sell-off location."""
    if location is SellOffLocation.CITY:
        percent = config.CITY_SELLOFF_DEDUCTION_PERCENT
    else:
        percent = config.HIDEOUT_SELLOFF_DEDUCTION_PERCENT
    return 1.0 - (percent / 100.0), percent


def calculate_ava_split(
    *,
    event: FarmEvent,
    total_item_value: int,
    total_repairs: int,
    total_silver_bags: int,
    map_cost: int,
    sell_off_location: SellOffLocation,
) -> AvaSplitResult:
    """Calculate per-player AVA payout using the configured formula."""
    player_count = event.filled_slots
    if player_count <= 0:
        raise ValueError(
            "No signed-up players found for this event. "
            "At least one slot must be filled before running a split."
        )

    item_pool = total_item_value - total_repairs
    if item_pool < 0:
        raise ValueError(
            f"Repairs ({format_silver(total_repairs)}) exceed total item value "
            f"({format_silver(total_item_value)})."
        )

    multiplier, deduction_percent = sell_off_multiplier(sell_off_location)
    item_after_sell_off = round(item_pool * multiplier)

    silver_pool = total_silver_bags - map_cost
    if silver_pool < 0:
        raise ValueError(
            f"Map cost ({format_silver(map_cost)}) exceeds total silver bags "
            f"({format_silver(total_silver_bags)})."
        )

    total_pool = item_after_sell_off + silver_pool
    per_player = round(total_pool / player_count)

    return AvaSplitResult(
        event_id=event.id,
        event_title=event.title,
        total_item_value=total_item_value,
        total_repairs=total_repairs,
        item_pool=item_pool,
        sell_off_location=sell_off_location,
        sell_off_deduction_percent=deduction_percent,
        item_after_sell_off=item_after_sell_off,
        total_silver_bags=total_silver_bags,
        map_cost=map_cost,
        silver_pool=silver_pool,
        total_pool=total_pool,
        player_count=player_count,
        per_player=per_player,
    )


def build_ava_split_embed(result: AvaSplitResult) -> discord.Embed:
    """Build the AVA loot split results embed."""
    embed = discord.Embed(
        title="💰 AVA Loot Split",
        description=f"**{result.event_title}**",
        colour=0xF1C40F,
    )

    lines = [
        f"**Total Item Value**\n{format_silver(result.total_item_value)}",
        f"**Repairs**\n{format_silver(result.total_repairs)}",
        f"**Item Pool After Repairs**\n{format_silver(result.item_pool)}",
        f"**Sell-Off Location**\n{result.sell_off_location.value}",
        f"**Sell-Off Percentage**\n{result.sell_off_deduction_percent:g}%",
        f"**Item Pool After Sell-Off**\n{format_silver(result.item_after_sell_off)}",
        f"**Silver Bags**\n{format_silver(result.total_silver_bags)}",
        f"**Map Cost**\n{format_silver(result.map_cost)}",
        f"**Silver Pool**\n{format_silver(result.silver_pool)}",
        "━━━━━━━━━━━━━━━━━━",
        f"**Total Pool**\n{format_silver(result.total_pool)}",
        f"**Players**\n{result.player_count}",
        "━━━━━━━━━━━━━━━━━━",
        f"**💵 Payout Per Player**\n{format_silver(result.per_player)} Silver",
    ]

    embed.add_field(name="\u200b", value="\n\n".join(lines), inline=False)
    return embed
