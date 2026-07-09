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


@dataclass(slots=True)
class FarmEvent:
    """A fame farm signup event."""

    id: int
    guild_id: int
    channel_id: int
    message_id: int
    thread_id: int
    creator_id: int
    title: str
    leaving_from: str
    map_type: str
    start_time: datetime
    requirements: str
    closed: bool
    slots: list[Slot] = field(default_factory=list)

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


class RolePanelType:
    """Role panel interaction type."""

    REACTION = "reaction"
    BUTTON = "button"


@dataclass(slots=True)
class RolePanelEntry:
    """Single emoji/button to role mapping."""

    id: int
    panel_id: int
    emoji_key: str
    role_id: int
    label: str
    sort_order: int


@dataclass(slots=True)
class RolePanel:
    """A reaction-role or button-role menu."""

    id: int
    guild_id: int
    channel_id: int
    message_id: int
    creator_id: int
    title: str
    description: str
    panel_type: str
    created_at: str
    entries: list[RolePanelEntry] = field(default_factory=list)

    def entry_by_emoji(self, emoji_key: str) -> RolePanelEntry | None:
        for entry in self.entries:
            if entry.emoji_key == emoji_key:
                return entry
        return None

    def entry_by_id(self, entry_id: int) -> RolePanelEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None
