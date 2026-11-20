"""Loot threads — opened when a raid starts; screenshots are read into a live split."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from models import FarmEvent, LootPlayer, LootSplit
from name_match import normalize_match_name
from roster_match import RosterEntry, match_names, roster_entry
from screenshot_reader import ScreenshotReader, ScreenshotReaderError
from split_calculator import format_silver, parse_silver_value
from utils import is_event_host

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

# Don't open loot threads for raids that started longer ago than this.
LOOT_THREAD_MAX_AGE = timedelta(hours=12)
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# On startup, re-check loot threads of raids this recent for screenshots missed while offline.
CATCH_UP_MAX_AGE = timedelta(days=3)
CATCH_UP_MESSAGES = 100


def _screenshot_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [
        a
        for a in message.attachments
        if (a.content_type or "").startswith("image/") and a.size <= MAX_ATTACHMENT_BYTES
    ]


# ── split math ───────────────────────────────────────────────────────────────


def sell_pct(split: LootSplit) -> float:
    """Sell-off cut for this raid (hideout default unless changed in the thread)."""
    return split.sell_pct if split.sell_pct is not None else config.HIDEOUT_SELLOFF_DEDUCTION_PERCENT


# Guild rule: the set lender rents out gear. They don't get a share of their own;
# instead every player who rented a set pays them this % of that player's share.
SET_LENDER_NAME = "notaeK"
SET_RENTAL_CUT_PERCENT = 15


def is_set_lender(player: LootPlayer) -> bool:
    return normalize_match_name(player.name) == normalize_match_name(SET_LENDER_NAME)


def compute_split(split: LootSplit) -> dict[str, Any]:
    """The split, following the guild's rules.

    pool      = (sold price, or market − sell-off %) − repair − map cost + silver bags
                (silver bags are never discounted)
    share     = pool ÷ players, not counting the set lender
    rented    = players who borrowed a set pay SET_RENTAL_CUT_PERCENT of their share
                to the set lender
    raid lead = also gets repair + map cost paid back
    """
    if split.sold_value is not None:
        after_tax: int | None = split.sold_value
    elif split.market_value is not None:
        after_tax = round(split.market_value * (1 - sell_pct(split) / 100))
    else:
        after_tax = None

    map_cost = split.map_cost or 0
    pool = None
    if after_tax is not None and split.repair_cost is not None:
        pool = after_tax - split.repair_cost - map_cost + split.silver_bags

    sharers = [p for p in split.players if not is_set_lender(p)]
    per_player = None
    if pool is not None and sharers:
        per_player = max(pool, 0) // len(sharers)

    rented = set(split.borrowers)
    borrowers = [p for p in sharers if normalize_match_name(p.name) in rented]
    rental_cut = (per_player or 0) * SET_RENTAL_CUT_PERCENT // 100
    return {
        "after_tax": after_tax,
        "pool": pool,
        "per_player": per_player,
        "sharers": sharers,
        "borrowers": borrowers,
        "rental_cut": rental_cut,
        "lender_total": rental_cut * len(borrowers),
        "reimbursement": (split.repair_cost or 0) + map_cost,
        "lender": next((p for p in split.players if is_set_lender(p)), None),
    }


def is_split_ready(split: LootSplit) -> bool:
    return compute_split(split)["per_player"] is not None


@dataclass(slots=True)
class PayoutEntry:
    """What one person is paid from a raid."""

    name: str
    user_id: int | None
    amount: int
    notes: list[str]
    icon: str = ""


def payout_entries(event: FarmEvent, split: LootSplit) -> list[PayoutEntry]:
    """Every person paid from this raid and exactly how much."""
    math = compute_split(split)
    per = math["per_player"] or 0
    borrowers = {id(p) for p in math["borrowers"]}
    lead_paid = False
    entries: list[PayoutEntry] = []
    for player in math["sharers"]:
        amount = per
        notes: list[str] = []
        if id(player) in borrowers:
            amount -= math["rental_cut"]
            notes.append(f"rented set −{SET_RENTAL_CUT_PERCENT}%")
        if player.user_id is not None and player.user_id == event.creator_id:
            amount += math["reimbursement"]
            notes.append("+ repair & map refund")
            lead_paid = True
        entries.append(PayoutEntry(player.name, player.user_id, amount, notes))

    if math["borrowers"]:
        lender = math["lender"]
        entries.append(
            PayoutEntry(
                lender.name if lender else SET_LENDER_NAME,
                lender.user_id if lender else None,
                math["lender_total"],
                [f"{SET_RENTAL_CUT_PERCENT}% from {len(math['borrowers'])} rented set(s), no share"],
                icon="🧥 ",
            )
        )
    if not lead_paid and math["reimbursement"]:
        entries.append(
            PayoutEntry(
                "raid lead",
                event.creator_id,
                math["reimbursement"],
                ["repair & map refund — not in the player list"],
                icon="👑 ",
            )
        )
    return entries


def payout_lines(event: FarmEvent, split: LootSplit) -> list[str]:
    """One line per person with exactly what they're paid."""
    lines: list[str] = []
    for entry in payout_entries(event, split):
        who = (
            f"<@{entry.user_id}> ({entry.name})"
            if entry.user_id
            else f"{entry.name} _(no Discord link)_"
        )
        note = f"  _({', '.join(entry.notes)})_" if entry.notes else ""
        lines.append(f"• {entry.icon}{who} — **{format_silver(entry.amount)}**{note}")
    return lines


def build_payout_embed(event: FarmEvent, split: LootSplit) -> discord.Embed:
    """Final per-person payout list, posted once the raid lead confirms."""
    math = compute_split(split)
    per = math["per_player"] or 0
    body = "\n".join(payout_lines(event, split))
    if len(body) > 3800:
        body = body[:3800] + "\n…"
    embed = discord.Embed(
        title=f"💵 Payout — {event.title}"[:256],
        description=(
            f"**{format_silver(per)} silver per share** · {len(math['sharers'])} players · "
            f"pool {format_silver(math['pool'] or 0)}\n\n{body}"
        ),
        colour=0x57F287,
    )
    embed.set_footer(text=f"Raid ID {event.id}  ·  confirmed by the raid lead")
    return embed


def build_split_embed(
    event: FarmEvent, split: LootSplit, *, review: bool = False
) -> discord.Embed:
    """The split breakdown. review=True shows the raid lead the payout before confirming."""
    pct = sell_pct(split)
    math = compute_split(split)
    waiting = "⏳ _waiting for screenshot_"

    def silver(value: int | None) -> str:
        return f"**{format_silver(value)}**" if value is not None else waiting

    market = silver(split.market_value)
    if split.market_value is not None and split.market_value_approx:
        market += "  ⚠️ _approximate — post the tooltip for the exact value_"

    lines = [
        f"📦 **Est. Market Value:** {market}",
        (
            f"🏷️ **Sold for:** {silver(math['after_tax'])}  _(set by the raid lead)_"
            if split.sold_value is not None
            else f"🏠 **After hideout −{pct:g}%:** {silver(math['after_tax'])}"
            if split.sell_pct is None
            else f"🏷️ **After sell-off −{pct:g}%:** {silver(math['after_tax'])}"
        ),
        f"🔧 **Repair cost:** {silver(split.repair_cost)}",
        f"🗺️ **Map cost:** **{format_silver(split.map_cost or 0)}**"
        + ("" if split.map_cost else "  _(press **Map cost** to add it)_"),
        f"💰 **Silver bags:** **{format_silver(split.silver_bags)}**  _(not discounted)_",
        "━━━━━━━━━━━━━━━━━━",
        f"🧮 **Total pool:** {silver(math['pool'])}  _(after repair & map)_",
        (
            f"👥 **Players:** **{len(math['sharers'])}**"
            + (f"  _({SET_LENDER_NAME} excluded — set lender)_" if math["lender"] else "")
            if split.players
            else f"👥 **Players:** {waiting}"
        ),
        (
            f"🧥 **Rented sets ({len(math['borrowers'])}):** "
            + ", ".join(p.name for p in math["borrowers"])
            + f"  _(−{SET_RENTAL_CUT_PERCENT}% each → {SET_LENDER_NAME})_"
            if math["borrowers"]
            else "🧥 **Rented sets:** none  _(press **Set borrowers** to tick who rented)_"
        ),
        f"👑 **Raid lead refund:** **{format_silver(math['reimbursement'])}**  _(repair + map)_",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if math["per_player"] is not None and not split.confirmed and review:
        lines.append(
            f"💵 **Share: {format_silver(math['per_player'])} silver** "
            f"× {len(math['sharers'])} players  _(not posted until you confirm)_"
        )
        if math["pool"] is not None and math["pool"] < 0:
            lines.append("⚠️ Repairs are larger than the loot value — payout is 0.")
        lines.append("")
        lines.append("**Each person gets:**")
        lines.extend(payout_lines(event, split))
    elif math["per_player"] is not None and not split.confirmed:
        lines.append(
            f"⏸️ **Payout:** waiting for the raid lead <@{event.creator_id}> to check "
            "the player list and confirm"
        )
    elif math["per_player"] is not None:
        lines.append(f"💵 **Share per player: {format_silver(math['per_player'])} silver** ✅")
        if math["pool"] is not None and math["pool"] < 0:
            lines.append("⚠️ Repairs are larger than the loot value — payout is 0.")
    else:
        missing = []
        if split.market_value is None and split.sold_value is None:
            missing.append("loot")
        if split.repair_cost is None:
            missing.append("repair")
        if not split.players:
            missing.append("party or DPS meter")
        lines.append(f"💵 **Payout per player:** needs {', '.join(missing)} screenshot")

    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:4000] + "\n…"
    embed = discord.Embed(
        title=f"💰 Loot Split — {event.title}"[:256],
        description=description,
        colour=0xF1C40F,
    )
    signed_up = [slot.user_id for slot in event.slots if slot.user_id is not None]
    if split.players:
        matched = [p for p in split.players if p.user_id is not None]
        extra = [p for p in split.players if p.user_id is None]
        parts = [f"✅ {p.name}" for p in matched] + [f"➕ {p.name}" for p in extra]
        names = ", ".join(parts)
        embed.add_field(
            name=f"👥 Players ({len(split.players)})  ·  ✅ signed up  ➕ not signed up",
            value=names if len(names) <= 1024 else names[:1000] + "…",
            inline=False,
        )
    if signed_up and (split.players or split.market_value is not None):
        in_split = {p.user_id for p in split.players}
        missing = [uid for uid in signed_up if uid not in in_split]
        if missing:
            text = " ".join(f"<@{uid}>" for uid in missing)
            embed.add_field(
                name=f"❗ Signed up but not in the split ({len(missing)})",
                value=text if len(text) <= 1024 else text[:1000] + "…",
                inline=False,
            )
    embed.add_field(
        name="📸 Post screenshots here",
        value=(
            "• **Loot** — the loot tab with the Est. Market Value tooltip showing\n"
            "• **Repair** — the *Repair all items* popup with the Total Cost\n"
            "• **Party** frames and/or the **DPS meter** — everyone listed gets a share\n"
            "I read them automatically and match names to the signups. "
            "Usually the same people? Hosts can press **Use signups**."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Raid ID {event.id}  ·  hosts can fix values with the buttons below")
    return embed


# ── panel buttons & modals ───────────────────────────────────────────────────


def _loot_cog(interaction: discord.Interaction) -> LootCog | None:
    return interaction.client.get_cog("LootCog")  # type: ignore[union-attr,return-value]


class LootPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"loot:(?P<action>roster|silver|sell|players|values|map|borrowers):(?P<event_id>\d+)",
):
    LABELS = {
        "roster": ("Use signups", "📋", discord.ButtonStyle.success),
        "silver": ("Silver bags", "💰", discord.ButtonStyle.primary),
        "sell": ("Sell price", "🏷️", discord.ButtonStyle.primary),
        "players": ("Edit players", "👥", discord.ButtonStyle.secondary),
        "values": ("Fix values", "✏️", discord.ButtonStyle.secondary),
        "map": ("Map cost", "🗺️", discord.ButtonStyle.primary),
        "borrowers": ("Set borrowers", "🧥", discord.ButtonStyle.primary),
    }

    def __init__(self, action: str, event_id: int) -> None:
        label, emoji, style = self.LABELS[action]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=f"loot:{action}:{event_id}"
            )
        )
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> LootPanelButton:
        return cls(match["action"], int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> Any:
        cog = _loot_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Try again in a moment.", ephemeral=True)
            return
        await cog.open_panel_modal(interaction, self.event_id, self.action)


def build_panel_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("roster", "silver", "sell", "players", "values", "map", "borrowers"):
        view.add_item(LootPanelButton(action, event_id))
    return view


class LootReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lootreview:(?P<action>confirm|link|players|borrowers):(?P<event_id>\d+)",
):
    LABELS = {
        "confirm": ("Confirm payout", "✅", discord.ButtonStyle.success),
        "link": ("Link names", "🔗", discord.ButtonStyle.primary),
        "players": ("Edit players", "👥", discord.ButtonStyle.secondary),
        "borrowers": ("Set borrowers", "🧥", discord.ButtonStyle.primary),
    }

    def __init__(self, action: str, event_id: int) -> None:
        label, emoji, style = self.LABELS[action]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"lootreview:{action}:{event_id}",
            )
        )
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> LootReviewButton:
        return cls(match["action"], int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> Any:
        cog = _loot_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Try again in a moment.", ephemeral=True)
            return
        await cog.handle_review(interaction, self.event_id, self.action)


def build_review_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("confirm", "borrowers", "link", "players"):
        view.add_item(LootReviewButton(action, event_id))
    return view


class LinkPlayerView(discord.ui.View):
    """Ephemeral picker: a name from the split + the Discord member it belongs to."""

    def __init__(self, cog: LootCog, event_id: int, split: LootSplit) -> None:
        super().__init__(timeout=300)
        self.cog, self.event_id = cog, event_id
        self.chosen_name: str | None = None
        self.chosen_member: discord.abc.User | None = None

        # Unlinked names first — those are the ones that usually need attention.
        ordered = sorted(split.players, key=lambda p: p.user_id is not None)[:25]
        self.name_select = discord.ui.Select(
            placeholder="Name from the split…",
            options=[
                discord.SelectOption(
                    label=p.name[:100],
                    value=p.name[:100],
                    emoji="✅" if p.user_id else "➕",
                    description="linked" if p.user_id else "not linked to anyone",
                )
                for p in ordered
            ],
            row=0,
        )
        self.name_select.callback = self._on_name  # type: ignore[method-assign]
        self.member_select = discord.ui.UserSelect(placeholder="…belongs to this member", row=1)
        self.member_select.callback = self._on_member  # type: ignore[method-assign]
        self.add_item(self.name_select)
        self.add_item(self.member_select)

    async def _try_link(self, interaction: discord.Interaction) -> None:
        if self.chosen_name and self.chosen_member:
            await self.cog.link_player(
                interaction, self.event_id, self.chosen_name, self.chosen_member
            )
        else:
            await interaction.response.defer()

    async def _on_name(self, interaction: discord.Interaction) -> None:
        self.chosen_name = self.name_select.values[0]
        await self._try_link(interaction)

    async def _on_member(self, interaction: discord.Interaction) -> None:
        self.chosen_member = self.member_select.values[0]
        await self._try_link(interaction)


class SilverBagsModal(discord.ui.Modal, title="Silver bags"):
    def __init__(self, cog: LootCog, event_id: int, split: LootSplit) -> None:
        super().__init__()
        self.cog, self.event_id = cog, event_id
        self.amount = discord.ui.TextInput(
            label="Total silver from bags", default=str(split.silver_bags), max_length=20
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = parse_silver_value("Silver bags", self.amount.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.apply_update(interaction, self.event_id, silver_bags=value)


class PlayersModal(discord.ui.Modal, title="Players in the split"):
    def __init__(self, cog: LootCog, event_id: int, split: LootSplit) -> None:
        super().__init__()
        self.cog, self.event_id = cog, event_id
        self.names = discord.ui.TextInput(
            label="One player name per line",
            style=discord.TextStyle.paragraph,
            default="\n".join(p.name for p in split.players)[:4000],
            required=False,
            max_length=4000,
        )
        self.add_item(self.names)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        names = [n.strip() for n in re.split(r"[\n,]+", self.names.value) if n.strip()]
        await self.cog.apply_update(interaction, self.event_id, players=names)


class ValuesModal(discord.ui.Modal, title="Fix loot values"):
    def __init__(self, cog: LootCog, event_id: int, split: LootSplit) -> None:
        super().__init__()
        self.cog, self.event_id = cog, event_id
        self.market = discord.ui.TextInput(
            label="Est. market value",
            default=str(split.market_value) if split.market_value is not None else None,
            required=False,
            max_length=20,
        )
        self.repair = discord.ui.TextInput(
            label="Repair cost",
            default=str(split.repair_cost) if split.repair_cost is not None else None,
            required=False,
            max_length=20,
        )
        self.add_item(self.market)
        self.add_item(self.repair)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        fields: dict[str, Any] = {}
        try:
            if self.market.value.strip():
                fields["market_value"] = parse_silver_value("Market value", self.market.value)
                fields["market_value_approx"] = 0
            if self.repair.value.strip():
                fields["repair_cost"] = parse_silver_value("Repair cost", self.repair.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.apply_update(interaction, self.event_id, **fields)


class MapCostModal(discord.ui.Modal, title="Map cost"):
    def __init__(self, cog: LootCog, event_id: int, split: LootSplit) -> None:
        super().__init__()
        self.cog, self.event_id = cog, event_id
        self.cost = discord.ui.TextInput(
            label="Map cost (paid back to the raid lead)",
            placeholder="e.g. 150,000 — leave blank for none",
            default=str(split.map_cost) if split.map_cost else None,
            required=False,
            max_length=20,
        )
        self.add_item(self.cost)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            cost = (
                parse_silver_value("Map cost", self.cost.value) if self.cost.value.strip() else 0
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await self.cog.apply_update(interaction, self.event_id, map_cost=cost)


class BorrowersView(discord.ui.View):
    """Private multi-select: tick the players who rented a set from the set lender."""

    def __init__(
        self, cog: LootCog, event_id: int, split: LootSplit, sharers: list[LootPlayer]
    ) -> None:
        super().__init__(timeout=300)
        self.cog, self.event_id = cog, event_id
        rented = set(split.borrowers)
        players = sharers[:25]
        self.select = discord.ui.Select(
            placeholder="Who rented a set?",
            min_values=0,
            max_values=len(players),
            options=[
                discord.SelectOption(
                    label=p.name[:100],
                    value=normalize_match_name(p.name)[:100],
                    default=normalize_match_name(p.name) in rented,
                )
                for p in players
            ],
        )
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_update(
            interaction, self.event_id, borrowers=list(self.select.values)
        )


class SellPriceModal(discord.ui.Modal, title="Sell price"):
    def __init__(self, cog: LootCog, event_id: int, split: LootSplit) -> None:
        super().__init__()
        self.cog, self.event_id = cog, event_id
        default_pct = config.HIDEOUT_SELLOFF_DEDUCTION_PERCENT
        self.pct = discord.ui.TextInput(
            label=f"Sell-off cut % (blank = hideout {default_pct:g}%)",
            placeholder=f"e.g. {config.CITY_SELLOFF_DEDUCTION_PERCENT:g} for city, 0 for none",
            default=f"{split.sell_pct:g}" if split.sell_pct is not None else None,
            required=False,
            max_length=6,
        )
        self.sold = discord.ui.TextInput(
            label="Actually sold for (optional)",
            placeholder="e.g. 45,000,000 — leave blank to use the estimate",
            default=str(split.sold_value) if split.sold_value is not None else None,
            required=False,
            max_length=20,
        )
        self.add_item(self.pct)
        self.add_item(self.sold)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        fields: dict[str, Any] = {"sell_pct": None, "sold_value": None}
        try:
            pct_text = self.pct.value.strip().rstrip("%").strip()
            if pct_text:
                pct = float(pct_text)
                if not 0 <= pct < 100:
                    raise ValueError("The sell-off cut must be between 0 and 99%.")
                fields["sell_pct"] = pct
            if self.sold.value.strip():
                fields["sold_value"] = parse_silver_value("Sold for", self.sold.value)
        except ValueError as exc:
            message = str(exc)
            if "could not convert" in message:
                message = "The sell-off cut must be a number, e.g. 20 or 15."
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return
        await self.cog.apply_update(interaction, self.event_id, **fields)


# ── cog ──────────────────────────────────────────────────────────────────────


class LootCog(commands.Cog):
    """Opens a loot thread when a raid starts and reads split screenshots."""


    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.reader: ScreenshotReader | None = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._catch_up_task: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LootPanelButton, LootReviewButton)
        try:
            # Loading the OCR models takes a moment; keep it off the event loop.
            self.reader = await asyncio.to_thread(ScreenshotReader)
        except Exception:
            logger.exception("OCR engine failed to load — loot screenshots won't be read")
        self.open_due_threads.start()
        self._catch_up_task = asyncio.create_task(self._catch_up_screenshots())

    async def cog_unload(self) -> None:
        self.open_due_threads.cancel()
        if self._catch_up_task:
            self._catch_up_task.cancel()
        self.bot.remove_dynamic_items(LootPanelButton, LootReviewButton)

    def _lock(self, event_id: int) -> asyncio.Lock:
        return self._locks.setdefault(event_id, asyncio.Lock())

    # ── thread creation ──────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def open_due_threads(self) -> None:
        """When a raid's start time passes: close signups, then open its loot thread."""
        now = datetime.now(timezone.utc)
        for event in await self.db.get_unstarted_events():
            if event.start_time > now:
                continue
            if now - event.start_time > LOOT_THREAD_MAX_AGE:
                await self.db.set_started(event.id, True)  # too old — leave it alone
                continue
            try:
                await self.start_raid(event)
            except Exception:
                logger.exception("Failed to start raid %s", event.id)

    async def start_raid(self, event: FarmEvent) -> str:
        """Close signups, refresh the post and open the loot thread. Returns a summary."""
        await self.db.set_started(event.id, True)
        parts: list[str] = []
        if not event.closed:
            await self.db.set_closed(event.id, True)
            event.closed = True
            raid_cog = self.bot.get_cog("RaidCog")
            if raid_cog is not None:
                await raid_cog._update_signup_message(event)  # type: ignore[attr-defined]
            parts.append("🔒 Signups closed.")
        if event.loot_thread_id:
            parts.append(f"Loot thread: <#{event.loot_thread_id}>")
        elif await self.db.get_loot_channel(event.guild_id) is None:
            parts.append("⚠️ No loot channel set — use `/ava settings loot_channel:`.")
        else:
            thread = await self.create_loot_thread(event)
            parts.append(
                f"💰 Loot thread opened: {thread.mention}" if thread else "⚠️ Couldn't open the loot thread."
            )
        logger.info("Raid %s started: %s", event.id, " ".join(parts))
        return " ".join(parts)

    @open_due_threads.before_loop
    async def _before_open(self) -> None:
        await self.bot.wait_until_ready()

    async def create_loot_thread(self, event: FarmEvent) -> discord.Thread | None:
        channel_id = await self.db.get_loot_channel(event.guild_id)
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Loot channel %s is not a text channel", channel_id)
            return None

        name = f"💰 Loot · {event.title}"[:90] + f" #{event.id}"
        thread = await channel.create_thread(
            name=name[:100],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,
        )
        # Record the thread first so the loop never opens a second one.
        await self.db.set_loot_thread(event.id, thread.id)

        split = await self.db.get_loot_split(event.id)
        panel = await thread.send(
            content=f"Raid **{event.title}** has started — post the loot screenshots here.",
            embed=build_split_embed(event, split),
            view=build_panel_view(event.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.db.update_loot_split(event.id, panel_message_id=panel.id)
        try:
            await panel.pin()
        except discord.HTTPException:
            pass
        logger.info("Opened loot thread %s for raid %s", thread.id, event.id)
        return thread

    # ── panel refresh ────────────────────────────────────────────────────

    async def refresh_panel(self, event: FarmEvent) -> None:
        split = await self.db.get_loot_split(event.id)
        if not event.loot_thread_id or not split.panel_message_id:
            return
        thread = await self._thread(event)
        if thread is None:
            return
        try:
            await thread.get_partial_message(split.panel_message_id).edit(
                embed=build_split_embed(event, split), view=build_panel_view(event.id)
            )
        except discord.HTTPException as exc:
            logger.warning("Could not update loot panel for raid %s: %s", event.id, exc)

    def _can_edit(self, member: discord.abc.User, event: FarmEvent) -> bool:
        return member.id == event.creator_id or (
            isinstance(member, discord.Member) and is_event_host(member)
        )

    async def open_panel_modal(
        self, interaction: discord.Interaction, event_id: int, action: str
    ) -> None:
        event = await self.db.get_event_by_id(event_id)
        if event is None:
            await interaction.response.send_message("This raid no longer exists.", ephemeral=True)
            return
        if not self._can_edit(interaction.user, event):
            await interaction.response.send_message(
                "Only the raid leader or an Event Host can change the split.", ephemeral=True
            )
            return
        if action == "roster":
            roster = await self._roster(event)
            if not roster:
                await interaction.response.send_message(
                    "Nobody is signed up for this raid.", ephemeral=True
                )
                return
            async with self._lock(event_id):
                await self.db.replace_loot_players(
                    event_id, [(entry.display, entry.user_id) for entry in roster]
                )
            await interaction.response.send_message(
                f"📋 Player list set to the **{len(roster)}** signed-up members. "
                "Use **Edit players** to add or remove anyone.",
                ephemeral=True,
            )
            await self.after_change(event_id)
            return
        split = await self.db.get_loot_split(event_id)
        if action == "borrowers":
            sharers = [p for p in split.players if not is_set_lender(p)]
            if not sharers:
                await interaction.response.send_message(
                    "No players in the split yet — post the party or DPS meter screenshot "
                    "(or press **Use signups**) first.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"🧥 **Tick everyone who rented a set from {SET_LENDER_NAME}** — they pay "
                f"{SET_RENTAL_CUT_PERCENT}% of their share to {SET_LENDER_NAME}. "
                "Untick everyone for none.",
                view=BorrowersView(self, event_id, split, sharers),
                ephemeral=True,
            )
            return
        modal_cls = {
            "silver": SilverBagsModal,
            "sell": SellPriceModal,
            "map": MapCostModal,
            "players": PlayersModal,
            "values": ValuesModal,
        }[action]
        await interaction.response.send_modal(modal_cls(self, event_id, split))

    async def _roster(self, event: FarmEvent) -> list[RosterEntry]:
        """Signed-up members (role slots only — not tentative/absent)."""
        guild = self.bot.get_guild(event.guild_id)
        entries: list[RosterEntry] = []
        for slot in sorted(event.slots, key=lambda s: s.slot_number):
            if slot.user_id is None:
                continue
            member: discord.Member | discord.User | None = None
            if guild is not None:
                member = guild.get_member(slot.user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(slot.user_id)
                    except discord.HTTPException:
                        member = None
            if member is None:
                try:
                    member = await self.bot.fetch_user(slot.user_id)
                except discord.HTTPException:
                    continue
            entries.append(roster_entry(member))
        return entries

    async def apply_update(
        self,
        interaction: discord.Interaction,
        event_id: int,
        *,
        players: list[str] | None = None,
        **fields: Any,
    ) -> None:
        async with self._lock(event_id):
            if fields:
                await self.db.update_loot_split(event_id, **fields)
            event = await self.db.get_event_by_id(event_id)
            if players is not None and event is not None:
                matched = match_names(players, await self._roster(event))
                await self.db.replace_loot_players(event_id, matched)
        await interaction.response.send_message("✅ Split updated.", ephemeral=True)
        if event:
            await self.after_change(event_id)

    # ── raid-lead review & confirmation ──────────────────────────────────

    async def _thread(self, event: FarmEvent) -> discord.Thread | None:
        if not event.loot_thread_id:
            return None
        try:
            thread = self.bot.get_channel(event.loot_thread_id) or await self.bot.fetch_channel(
                event.loot_thread_id
            )
        except discord.HTTPException:
            return None
        return thread if isinstance(thread, discord.Thread) else None

    async def after_change(self, event_id: int) -> None:
        """Call after anything in the split changes.

        Undoes a previous confirmation (the lead must re-check), refreshes the panel,
        and pings the raid lead once the split has everything it needs.
        """
        async with self._lock(event_id):
            event = await self.db.get_event_by_id(event_id)
            if event is None:
                return
            split = await self.db.get_loot_split(event_id)
            if split.confirmed:
                await self.db.update_loot_split(
                    event_id, confirmed=0, review_message_id=None
                )
                # The numbers changed: take the old payout back off balances until the
                # raid lead confirms again.
                await self.db.set_payout_credits(event.guild_id, event_id, [], 0)
                split = await self.db.get_loot_split(event_id)

            ping_needed = is_split_ready(split) and split.review_message_id is None
            if ping_needed:
                thread = await self._thread(event)
                if thread is not None:
                    review = await thread.send(
                        content=(
                            f"<@{event.creator_id}> the loot split is ready — please check the "
                            "players before the payout is shown.\n"
                            "• **Link names** to connect a name to the right Discord member\n"
                            "• **Edit players** to add or remove someone\n"
                            "• **Confirm payout** when the list is right"
                        ),
                        embed=build_split_embed(event, split, review=True),
                        view=build_review_view(event_id),
                        allowed_mentions=discord.AllowedMentions(
                            users=[discord.Object(id=event.creator_id)]
                        ),
                    )
                    await self.db.update_loot_split(event_id, review_message_id=review.id)
            elif split.review_message_id is not None:
                # Keep the lead's review message showing the current numbers/players.
                thread = await self._thread(event)
                if thread is not None:
                    try:
                        await thread.get_partial_message(split.review_message_id).edit(
                            embed=build_split_embed(event, split, review=True)
                        )
                    except discord.HTTPException:
                        pass
        await self.refresh_panel(event)

    async def handle_review(
        self, interaction: discord.Interaction, event_id: int, action: str
    ) -> None:
        event = await self.db.get_event_by_id(event_id)
        if event is None:
            await interaction.response.send_message("This raid no longer exists.", ephemeral=True)
            return
        if not self._can_edit(interaction.user, event):
            await interaction.response.send_message(
                "Only the raid lead or an Event Host can do that.", ephemeral=True
            )
            return
        if action in ("players", "borrowers"):
            await self.open_panel_modal(interaction, event_id, action)
            return
        if action == "link":
            split = await self.db.get_loot_split(event_id)
            if not split.players:
                await interaction.response.send_message("No players yet.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Pick a name from the split, then the Discord member it belongs to:",
                view=LinkPlayerView(self, event_id, split),
                ephemeral=True,
            )
            return

        # confirm
        async with self._lock(event_id):
            split = await self.db.get_loot_split(event_id)
            if not is_split_ready(split):
                await interaction.response.send_message(
                    "The split isn't complete yet (needs loot, repair and players).",
                    ephemeral=True,
                )
                return
            if split.confirmed:
                await interaction.response.send_message("Already confirmed.", ephemeral=True)
                return
            await self.db.update_loot_split(event_id, confirmed=1)
            split = await self.db.get_loot_split(event_id)
            # Credit everyone's AVA balance (replacing any earlier credits for this raid,
            # so re-confirming after a change never counts twice).
            entries = payout_entries(event, split)
            credits: dict[int, int] = {}
            for entry in entries:
                if entry.user_id:
                    credits[entry.user_id] = credits.get(entry.user_id, 0) + entry.amount
            await self.db.set_payout_credits(
                event.guild_id, event_id, list(credits.items()), interaction.user.id
            )
            unlinked = [e.name for e in entries if not e.user_id and e.amount]

        await interaction.response.send_message(
            f"✅ Payout confirmed — added to **{len(credits)}** balance(s)."
            + (
                "\n⚠️ Not added (no Discord link): " + ", ".join(f"**{n}**" for n in unlinked)
                + ". Use **Link names**, or an admin can `/bal-add` them."
                if unlinked
                else ""
            ),
            ephemeral=True,
        )
        if interaction.message is not None:
            try:
                await interaction.message.edit(
                    content=f"✅ Player list confirmed by {interaction.user.mention}.",
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
        thread = await self._thread(event)
        if thread is not None:
            await thread.send(
                embed=build_payout_embed(event, split),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.refresh_panel(event)

    async def link_player(
        self, interaction: discord.Interaction, event_id: int, name: str, member: discord.abc.User
    ) -> None:
        async with self._lock(event_id):
            split = await self.db.get_loot_split(event_id)
            clash = next(
                (p for p in split.players if p.user_id == member.id and p.name.lower() != name.lower()),
                None,
            )
            if clash is not None:
                await interaction.response.send_message(
                    f"{member.mention} is already in the split as **{clash.name}**. "
                    "Remove that entry with **Edit players** first.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await self.db.link_loot_player(event_id, name, member.id)
        await interaction.response.edit_message(
            content=f"🔗 **{name}** → {member.mention}", view=None
        )
        await self.after_change(event_id)

    # ── screenshots ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        if not _screenshot_attachments(message):
            return
        event = await self.db.get_event_by_loot_thread(message.channel.id)
        if event is None:
            return
        await self._handle_screenshots(message, event)

    async def _catch_up_screenshots(self) -> None:
        """Read screenshots posted in open loot threads while the bot was offline."""
        await self.bot.wait_until_ready()
        cutoff = datetime.now(timezone.utc) - CATCH_UP_MAX_AGE
        handled = 0
        for event in await self.db.get_active_events():
            if not event.loot_thread_id or event.start_time < cutoff:
                continue
            if (await self.db.get_loot_split(event.id)).confirmed:
                continue
            try:
                thread = self.bot.get_channel(event.loot_thread_id) or await self.bot.fetch_channel(
                    event.loot_thread_id
                )
                if not isinstance(thread, discord.Thread):
                    continue
                missed = [
                    msg
                    async for msg in thread.history(limit=CATCH_UP_MESSAGES)
                    if not msg.author.bot
                    and _screenshot_attachments(msg)
                    # Anything the bot already looked at carries one of its reactions.
                    and not any(reaction.me for reaction in msg.reactions)
                ]
            except discord.HTTPException as exc:
                logger.warning("Couldn't check loot thread for raid %s: %s", event.id, exc)
                continue
            for msg in reversed(missed):  # oldest first
                try:
                    await self._handle_screenshots(msg, event)
                    handled += 1
                except Exception:
                    logger.exception("Failed to catch up screenshot %s", msg.id)
        if handled:
            logger.info("Caught up on %d screenshot message(s) posted while offline", handled)

    async def _handle_screenshots(self, message: discord.Message, event: FarmEvent) -> None:
        images = _screenshot_attachments(message)

        if self.reader is None:
            await message.reply(
                "The screenshot reader isn't working right now (check the bot log). "
                "Hosts can enter values with the panel buttons meanwhile.",
                mention_author=False,
            )
            return

        try:
            await message.add_reaction("🔍")
        except discord.HTTPException:
            pass

        lines: list[str] = []
        changed = False
        async with self._lock(event.id):
            for attachment in images:
                line, did_change = await self._process_image(event.id, attachment)
                lines.append(line)
                changed = changed or did_change

        try:
            await message.remove_reaction("🔍", self.bot.user)  # type: ignore[arg-type]
            await message.add_reaction("✅" if changed else "❓")
        except discord.HTTPException:
            pass
        await message.reply("\n".join(lines), mention_author=False)
        if changed:
            await self.after_change(event.id)

    async def _process_image(
        self, event_id: int, attachment: discord.Attachment
    ) -> tuple[str, bool]:
        assert self.reader is not None
        try:
            reading = await self.reader.read(await attachment.read())
        except ScreenshotReaderError as exc:
            return f"⚠️ `{attachment.filename}`: {exc}", False
        except discord.HTTPException:
            return f"⚠️ `{attachment.filename}`: couldn't download it from Discord.", False

        note = f"  _({reading.note})_" if reading.note else ""
        split = await self.db.get_loot_split(event_id)

        if reading.kind == "loot" and reading.market_value is not None:
            await self.db.update_loot_split(
                event_id,
                market_value=reading.market_value,
                market_value_approx=int(reading.market_value_is_approximate),
            )
            approx = " (approximate)" if reading.market_value_is_approximate else ""
            old = (
                f" — replaced {format_silver(split.market_value)}"
                if split.market_value is not None
                else ""
            )
            return (
                f"📦 Loot: market value **{format_silver(reading.market_value)}**{approx}{old}{note}",
                True,
            )
        if reading.kind == "repair" and reading.repair_cost is not None:
            await self.db.update_loot_split(event_id, repair_cost=reading.repair_cost)
            old = (
                f" — replaced {format_silver(split.repair_cost)}"
                if split.repair_cost is not None
                else ""
            )
            return f"🔧 Repair cost: **{format_silver(reading.repair_cost)}**{old}{note}", True
        if reading.kind in ("party", "dps") and reading.players:
            event = await self.db.get_event_by_id(event_id)
            roster = await self._roster(event) if event else []
            matched = match_names(reading.players, roster)
            added = await self.db.add_loot_players(event_id, matched)
            total = len(split.players) + len(added)
            label = "Party" if reading.kind == "party" else "DPS meter"
            if not added:
                return f"👥 {label}: everyone was already counted ({total} players){note}", False

            read_as = dict(zip((name for name, _ in matched), reading.players))
            parts = []
            for player in added:
                original = read_as.get(player.name, player.name)
                fixed = f" _(read as {original})_" if original != player.name else ""
                if player.user_id is not None:
                    parts.append(f"✅ {player.name}{fixed}")
                else:
                    parts.append(f"➕ {player.name} _(not signed up)_")
            in_split = {p.user_id for p in split.players} | {p.user_id for p in added}
            missing = sum(1 for entry in roster if entry.user_id not in in_split)
            tail = f" · {missing} signup(s) not seen yet" if missing else " · all signups found"
            return (
                f"👥 {label}: added {', '.join(parts)} — **{total} players**{tail}{note}",
                True,
            )
        if reading.kind == "other":
            return (
                f"❓ `{attachment.filename}` doesn't look like a loot, repair, party or "
                f"DPS-meter screenshot.{note}",
                False,
            )
        return f"⚠️ `{attachment.filename}`: recognised as {reading.kind} but couldn't read it.{note}", False


async def setup(bot: commands.Bot) -> None:
    raise RuntimeError("Load LootCog from bot.py with a shared Database instance.")
