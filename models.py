"""Domain models for fame farm signup events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Slot:
    """A single role slot within an event."""

    slot_number: int
    role_name: str
    user_id: int | None = None
    emoji: str | None = None


@dataclass(slots=True)
class RoleGroup:
    """All slots of one role name within an event (e.g. the 5 DPS slots)."""

    role_name: str
    emoji: str
    slots: list[Slot] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.slots)

    @property
    def filled(self) -> int:
        return sum(1 for slot in self.slots if slot.user_id is not None)

    @property
    def is_full(self) -> bool:
        return self.filled >= self.total

    def first_open(self) -> Slot | None:
        for slot in self.slots:
            if slot.user_id is None:
                return slot
        return None


class SignupStatus:
    """Non-roster signup states shown under the roster."""

    TENTATIVE = "tentative"
    ABSENCE = "absence"
    ALL = (TENTATIVE, ABSENCE)


@dataclass(slots=True)
class FarmEvent:
    """A fame farm signup event."""

    id: int
    guild_id: int
    channel_id: int
    message_id: int
    thread_id: int | None
    creator_id: int
    title: str
    leaving_from: str
    map_type: str
    start_time: datetime
    requirements: str
    closed: bool
    slots: list[Slot] = field(default_factory=list)
    # user_id -> SignupStatus value (absence)
    statuses: dict[int, str] = field(default_factory=dict)
    loot_thread_id: int | None = None
    colour: int | None = None

    @property
    def total_slots(self) -> int:
        return len(self.slots)

    @property
    def filled_slots(self) -> int:
        return sum(1 for slot in self.slots if slot.user_id is not None)

    def slot_by_number(self, number: int) -> Slot | None:
        for slot in self.slots:
            if slot.slot_number == number:
                return slot
        return None

    def slot_for_user(self, user_id: int) -> Slot | None:
        for slot in self.slots:
            if slot.user_id == user_id:
                return slot
        return None

    def role_groups(self) -> list[RoleGroup]:
        """Group slots by role name, in slot order."""
        from raid_presets import default_role_emoji, is_generic_emoji
        from role_icons import icon_for_role

        groups: dict[str, RoleGroup] = {}
        for slot in sorted(self.slots, key=lambda s: s.slot_number):
            group = groups.get(slot.role_name)
            if group is None:
                # A preset's own icon wins; generic placeholder emojis are upgraded
                # to the built-in Albion icons when the role name matches one.
                own = slot.emoji if slot.emoji and not is_generic_emoji(slot.emoji) else None
                group = RoleGroup(
                    role_name=slot.role_name,
                    emoji=own
                    or icon_for_role(slot.role_name)
                    or slot.emoji
                    or default_role_emoji(slot.role_name),
                )
                groups[slot.role_name] = group
            group.slots.append(slot)
        return list(groups.values())

    def role_group(self, role_name: str) -> RoleGroup | None:
        for group in self.role_groups():
            if group.role_name.lower() == role_name.lower():
                return group
        return None

    def users_with_status(self, status: str) -> list[int]:
        return [uid for uid, value in self.statuses.items() if value == status]


@dataclass(slots=True)
class RaidPreset:
    """A saved raid template: roles, default title, map type and requirements."""

    id: int
    guild_id: int
    name: str
    title: str
    map_type: str
    roles: str
    requirements: str
    creator_id: int
    image_path: str | None = None
    colour: int | None = None


@dataclass(slots=True)
class AvaSplitRecord:
    """Persisted AVA split calculation for audit history."""

    id: int
    event_id: int
    guild_id: int
    channel_id: int
    calculated_by: int
    total_item_value: int
    total_repairs: int
    total_silver_bags: int
    map_cost: int
    sell_off_location: str
    sell_off_deduction_percent: float
    item_pool: int
    item_after_sell_off: int
    silver_pool: int
    total_pool: int
    player_count: int
    per_player: int
    created_at: str


@dataclass(slots=True)
class LootPlayer:
    """A player in a loot split, optionally matched to a raid signup."""

    name: str
    user_id: int | None = None


@dataclass(slots=True)
class LootSplit:
    """Screenshot-driven loot split state for one raid's loot thread."""

    event_id: int
    market_value: int | None = None
    market_value_approx: bool = False
    repair_cost: int | None = None
    silver_bags: int = 0
    panel_message_id: int | None = None
    review_message_id: int | None = None
    confirmed: bool = False
    players: list[LootPlayer] = field(default_factory=list)
    sell_pct: float | None = None  # sell-off cut for this raid; None = hideout default
    sold_value: int | None = None  # what the loot actually sold for (overrides the estimate)
    map_cost: int = 0  # paid back to the raid lead with the repair cost
    borrowers: list[str] = field(default_factory=list)  # normalized names that rented a set


@dataclass(slots=True)
class GuildSettings:
    """Server-wide AVA settings."""

    guild_id: int
    raid_channel_id: int | None = None
    loot_channel_id: int | None = None
    ping_role_id: int | None = None  # None = default from config, 0 = don't ping
    timezone: str | None = None  # IANA name used for times typed without one
