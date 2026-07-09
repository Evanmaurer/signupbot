"""SQLite persistence layer for signup events."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from models import AvaSplitRecord, FarmEvent, RolePanel, RolePanelEntry, Slot
from split_calculator import AvaSplitResult
from utils import ensure_timezone_aware

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    thread_id INTEGER NOT NULL UNIQUE,
    creator_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    leaving_from TEXT NOT NULL,
    map_type TEXT NOT NULL,
    start_time TEXT NOT NULL,
    requirements TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    slot_number INTEGER NOT NULL,
    role_name TEXT NOT NULL,
    user_id INTEGER,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    UNIQUE (event_id, slot_number)
);

CREATE INDEX IF NOT EXISTS idx_events_thread_id ON events(thread_id);
CREATE INDEX IF NOT EXISTS idx_events_message_id ON events(message_id);
CREATE INDEX IF NOT EXISTS idx_events_channel_id ON events(channel_id);
CREATE INDEX IF NOT EXISTS idx_slots_event_id ON slots(event_id);

CREATE TABLE IF NOT EXISTS ava_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    calculated_by INTEGER NOT NULL,
    total_item_value INTEGER NOT NULL,
    total_repairs INTEGER NOT NULL,
    total_silver_bags INTEGER NOT NULL,
    map_cost INTEGER NOT NULL,
    sell_off_location TEXT NOT NULL,
    sell_off_deduction_percent REAL NOT NULL,
    item_pool INTEGER NOT NULL,
    item_after_sell_off INTEGER NOT NULL,
    silver_pool INTEGER NOT NULL,
    total_pool INTEGER NOT NULL,
    player_count INTEGER NOT NULL,
    per_player INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ava_splits_event_id ON ava_splits(event_id);

CREATE TABLE IF NOT EXISTS role_panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    creator_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    panel_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS role_panel_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL,
    emoji_key TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    FOREIGN KEY (panel_id) REFERENCES role_panels(id) ON DELETE CASCADE,
    UNIQUE (panel_id, emoji_key)
);

CREATE INDEX IF NOT EXISTS idx_role_panels_guild_id ON role_panels(guild_id);
CREATE INDEX IF NOT EXISTS idx_role_panels_message_id ON role_panels(message_id);
CREATE INDEX IF NOT EXISTS idx_role_panel_entries_panel_id ON role_panel_entries(panel_id);
"""


class Database:
    """Async SQLite database for farm signup events."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA foreign_keys = ON")
        await self._connection.executescript(_SCHEMA)
        await self._connection.commit()
        logger.info("Connected to database at %s", self._path)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            logger.info("Database connection closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection

    async def create_event(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        thread_id: int,
        creator_id: int,
        title: str,
        leaving_from: str,
        map_type: str,
        start_time: datetime,
        requirements: str,
        slots: list[tuple[int, str]],
    ) -> FarmEvent:
        cursor = await self.conn.execute(
            """
            INSERT INTO events (
                guild_id, channel_id, message_id, thread_id, creator_id,
                title, leaving_from, map_type, start_time, requirements
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                thread_id,
                creator_id,
                title,
                leaving_from,
                map_type,
                start_time.isoformat(),
                requirements,
            ),
        )
        event_id = cursor.lastrowid
        assert event_id is not None

        slot_models: list[Slot] = []
        for slot_number, role_name in slots:
            await self.conn.execute(
                """
                INSERT INTO slots (event_id, slot_number, role_name)
                VALUES (?, ?, ?)
                """,
                (event_id, slot_number, role_name),
            )
            slot_models.append(Slot(slot_number=slot_number, role_name=role_name))

        await self.conn.commit()
        return FarmEvent(
            id=event_id,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            thread_id=thread_id,
            creator_id=creator_id,
            title=title,
            leaving_from=leaving_from,
            map_type=map_type,
            start_time=start_time,
            requirements=requirements,
            closed=False,
            slots=slot_models,
        )

    async def _row_to_event(self, row: aiosqlite.Row) -> FarmEvent:
        slot_rows = await self.conn.execute_fetchall(
            """
            SELECT slot_number, role_name, user_id
            FROM slots
            WHERE event_id = ?
            ORDER BY slot_number
            """,
            (row["id"],),
        )
        slots = [
            Slot(
                slot_number=slot_row["slot_number"],
                role_name=slot_row["role_name"],
                user_id=slot_row["user_id"],
            )
            for slot_row in slot_rows
        ]
        return FarmEvent(
            id=row["id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            thread_id=row["thread_id"],
            creator_id=row["creator_id"],
            title=row["title"],
            leaving_from=row["leaving_from"],
            map_type=row["map_type"],
            start_time=ensure_timezone_aware(datetime.fromisoformat(row["start_time"])),
            requirements=row["requirements"],
            closed=bool(row["closed"]),
            slots=slots,
        )

    async def get_event_by_id(self, event_id: int) -> FarmEvent | None:
        row = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE id = ?",
            (event_id,),
        )
        if not row:
            return None
        return await self._row_to_event(row[0])

    async def get_event_by_thread(self, thread_id: int) -> FarmEvent | None:
        row = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE thread_id = ?",
            (thread_id,),
        )
        if not row:
            return None
        return await self._row_to_event(row[0])

    async def get_event_by_message(self, message_id: int) -> FarmEvent | None:
        row = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE message_id = ?",
            (message_id,),
        )
        if not row:
            return None
        return await self._row_to_event(row[0])

    async def get_events_by_channel(self, channel_id: int) -> list[FarmEvent]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE channel_id = ? ORDER BY id",
            (channel_id,),
        )
        return [await self._row_to_event(row) for row in rows]

    async def get_active_events(self) -> list[FarmEvent]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events ORDER BY id"
        )
        return [await self._row_to_event(row) for row in rows]

    async def set_slot_user(
        self, event_id: int, slot_number: int, user_id: int | None
    ) -> None:
        await self.conn.execute(
            """
            UPDATE slots
            SET user_id = ?
            WHERE event_id = ? AND slot_number = ?
            """,
            (user_id, event_id, slot_number),
        )
        await self.conn.commit()

    async def clear_user_from_event(self, event_id: int, user_id: int) -> int | None:
        """Remove a user from whichever slot they occupy. Returns freed slot number."""
        row = await self.conn.execute_fetchall(
            """
            SELECT slot_number FROM slots
            WHERE event_id = ? AND user_id = ?
            """,
            (event_id, user_id),
        )
        if not row:
            return None
        slot_number = row[0]["slot_number"]
        await self.set_slot_user(event_id, slot_number, None)
        return slot_number

    async def set_closed(self, event_id: int, closed: bool) -> None:
        await self.conn.execute(
            "UPDATE events SET closed = ? WHERE id = ?",
            (int(closed), event_id),
        )
        await self.conn.commit()

    async def update_event_fields(
        self,
        event_id: int,
        *,
        title: str | None = None,
        leaving_from: str | None = None,
        map_type: str | None = None,
        start_time: datetime | None = None,
        requirements: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if leaving_from is not None:
            updates["leaving_from"] = leaving_from
        if map_type is not None:
            updates["map_type"] = map_type
        if start_time is not None:
            updates["start_time"] = start_time.isoformat()
        if requirements is not None:
            updates["requirements"] = requirements

        if not updates:
            return

        set_clause = ", ".join(f"{column} = ?" for column in updates)
        values = list(updates.values()) + [event_id]
        await self.conn.execute(
            f"UPDATE events SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def delete_event(self, event_id: int) -> None:
        await self.conn.execute("DELETE FROM ava_splits WHERE event_id = ?", (event_id,))
        await self.conn.execute("DELETE FROM slots WHERE event_id = ?", (event_id,))
        await self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await self.conn.commit()

    async def save_ava_split(
        self,
        result: AvaSplitResult,
        *,
        guild_id: int,
        channel_id: int,
        calculated_by: int,
    ) -> AvaSplitRecord:
        cursor = await self.conn.execute(
            """
            INSERT INTO ava_splits (
                event_id, guild_id, channel_id, calculated_by,
                total_item_value, total_repairs, total_silver_bags, map_cost,
                sell_off_location, sell_off_deduction_percent,
                item_pool, item_after_sell_off, silver_pool, total_pool,
                player_count, per_player
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.event_id,
                guild_id,
                channel_id,
                calculated_by,
                result.total_item_value,
                result.total_repairs,
                result.total_silver_bags,
                result.map_cost,
                result.sell_off_location.value,
                result.sell_off_deduction_percent,
                result.item_pool,
                result.item_after_sell_off,
                result.silver_pool,
                result.total_pool,
                result.player_count,
                result.per_player,
            ),
        )
        record_id = cursor.lastrowid
        assert record_id is not None
        await self.conn.commit()

        row = await self.conn.execute_fetchall(
            "SELECT * FROM ava_splits WHERE id = ?",
            (record_id,),
        )
        return self._row_to_split_record(row[0])

    def _row_to_split_record(self, row: aiosqlite.Row) -> AvaSplitRecord:
        return AvaSplitRecord(
            id=row["id"],
            event_id=row["event_id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            calculated_by=row["calculated_by"],
            total_item_value=row["total_item_value"],
            total_repairs=row["total_repairs"],
            total_silver_bags=row["total_silver_bags"],
            map_cost=row["map_cost"],
            sell_off_location=row["sell_off_location"],
            sell_off_deduction_percent=row["sell_off_deduction_percent"],
            item_pool=row["item_pool"],
            item_after_sell_off=row["item_after_sell_off"],
            silver_pool=row["silver_pool"],
            total_pool=row["total_pool"],
            player_count=row["player_count"],
            per_player=row["per_player"],
            created_at=row["created_at"],
        )

    async def _load_panel_entries(self, panel_id: int) -> list[RolePanelEntry]:
        rows = await self.conn.execute_fetchall(
            """
            SELECT id, panel_id, emoji_key, role_id, label, sort_order
            FROM role_panel_entries
            WHERE panel_id = ?
            ORDER BY sort_order
            """,
            (panel_id,),
        )
        return [
            RolePanelEntry(
                id=row["id"],
                panel_id=row["panel_id"],
                emoji_key=row["emoji_key"],
                role_id=row["role_id"],
                label=row["label"],
                sort_order=row["sort_order"],
            )
            for row in rows
        ]

    async def _row_to_panel(self, row: aiosqlite.Row) -> RolePanel:
        entries = await self._load_panel_entries(row["id"])
        return RolePanel(
            id=row["id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            creator_id=row["creator_id"],
            title=row["title"],
            description=row["description"],
            panel_type=row["panel_type"],
            created_at=row["created_at"],
            entries=entries,
        )

    async def create_role_panel(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        creator_id: int,
        title: str,
        description: str,
        panel_type: str,
        entries: list[tuple[str, int, str]],
    ) -> RolePanel:
        cursor = await self.conn.execute(
            """
            INSERT INTO role_panels (
                guild_id, channel_id, message_id, creator_id,
                title, description, panel_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                creator_id,
                title,
                description,
                panel_type,
            ),
        )
        panel_id = cursor.lastrowid
        assert panel_id is not None

        for sort_order, (emoji_key, role_id, label) in enumerate(entries):
            await self.conn.execute(
                """
                INSERT INTO role_panel_entries (
                    panel_id, emoji_key, role_id, label, sort_order
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (panel_id, emoji_key, role_id, label, sort_order),
            )

        await self.conn.commit()
        panel = await self.get_role_panel_by_id(panel_id)
        assert panel is not None
        return panel

    async def get_role_panel_by_id(self, panel_id: int) -> RolePanel | None:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM role_panels WHERE id = ?",
            (panel_id,),
        )
        if not rows:
            return None
        return await self._row_to_panel(rows[0])

    async def get_role_panel_by_message(self, message_id: int) -> RolePanel | None:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM role_panels WHERE message_id = ?",
            (message_id,),
        )
        if not rows:
            return None
        return await self._row_to_panel(rows[0])

    async def get_role_panels_by_guild(self, guild_id: int) -> list[RolePanel]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM role_panels WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        )
        return [await self._row_to_panel(row) for row in rows]

    async def get_role_panels_by_channel(self, channel_id: int) -> list[RolePanel]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM role_panels WHERE channel_id = ? ORDER BY id",
            (channel_id,),
        )
        return [await self._row_to_panel(row) for row in rows]

    async def get_button_role_panels(self) -> list[RolePanel]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM role_panels WHERE panel_type = 'button' ORDER BY id"
        )
        return [await self._row_to_panel(row) for row in rows]

    async def update_role_panel(
        self,
        panel_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if not updates:
            return
        set_clause = ", ".join(f"{column} = ?" for column in updates)
        values = list(updates.values()) + [panel_id]
        await self.conn.execute(
            f"UPDATE role_panels SET {set_clause} WHERE id = ?",
            values,
        )
        await self.conn.commit()

    async def add_role_panel_entries(
        self,
        panel_id: int,
        entries: list[tuple[str, int, str]],
    ) -> None:
        existing = await self._load_panel_entries(panel_id)
        next_order = len(existing)
        for emoji_key, role_id, label in entries:
            await self.conn.execute(
                """
                INSERT INTO role_panel_entries (
                    panel_id, emoji_key, role_id, label, sort_order
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (panel_id, emoji_key, role_id, label, next_order),
            )
            next_order += 1
        await self.conn.commit()

    async def remove_role_panel_entries_by_emoji(
        self, panel_id: int, emoji_keys: list[str]
    ) -> int:
        removed = 0
        for emoji_key in emoji_keys:
            cursor = await self.conn.execute(
                """
                DELETE FROM role_panel_entries
                WHERE panel_id = ? AND emoji_key = ?
                """,
                (panel_id, emoji_key),
            )
            removed += cursor.rowcount
        await self.conn.commit()
        await self._reindex_panel_entries(panel_id)
        return removed

    async def _reindex_panel_entries(self, panel_id: int) -> None:
        entries = await self._load_panel_entries(panel_id)
        for order, entry in enumerate(entries):
            await self.conn.execute(
                "UPDATE role_panel_entries SET sort_order = ? WHERE id = ?",
                (order, entry.id),
            )
        await self.conn.commit()

    async def delete_role_panel(self, panel_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM role_panel_entries WHERE panel_id = ?",
            (panel_id,),
        )
        await self.conn.execute("DELETE FROM role_panels WHERE id = ?", (panel_id,))
        await self.conn.commit()
