"""SQLite persistence layer for signup events."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from models import (
    AvaSplitRecord,
    FarmEvent,
    GuildSettings,
    LootPlayer,
    LootSplit,
    RaidPreset,
    Slot,
)
from split_calculator import AvaSplitResult
from utils import ensure_timezone_aware

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    thread_id INTEGER UNIQUE,
    creator_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    leaving_from TEXT NOT NULL,
    map_type TEXT NOT NULL,
    start_time TEXT NOT NULL,
    requirements TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    loot_thread_id INTEGER,
    started INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    slot_number INTEGER NOT NULL,
    role_name TEXT NOT NULL,
    user_id INTEGER,
    emoji TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    UNIQUE (event_id, slot_number)
);

CREATE TABLE IF NOT EXISTS event_statuses (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, user_id),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    loot_channel_id INTEGER,
    raid_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS loot_splits (
    event_id INTEGER PRIMARY KEY,
    market_value INTEGER,
    market_value_approx INTEGER NOT NULL DEFAULT 0,
    repair_cost INTEGER,
    silver_bags INTEGER NOT NULL DEFAULT 0,
    panel_message_id INTEGER,
    review_message_id INTEGER,
    confirmed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS loot_players (
    event_id INTEGER NOT NULL,
    normalized TEXT NOT NULL,
    name TEXT NOT NULL,
    user_id INTEGER,
    PRIMARY KEY (event_id, normalized),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS raid_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    title TEXT NOT NULL,
    map_type TEXT NOT NULL,
    roles TEXT NOT NULL,
    requirements TEXT NOT NULL,
    creator_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guild_id, name)
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
        await self._migrate()
        await self._connection.executescript(_SCHEMA)
        await self._connection.commit()
        logger.info("Connected to database at %s", self._path)

    async def _migrate(self) -> None:
        """Upgrade databases created by the thread-signup version of the bot."""
        conn = self.conn

        slot_cols = {
            row["name"] for row in await conn.execute_fetchall("PRAGMA table_info(slots)")
        }
        if "emoji" not in slot_cols:
            await conn.execute("ALTER TABLE slots ADD COLUMN emoji TEXT")
            await conn.commit()
            logger.info("Migration: added slots.emoji column")

        split_cols = {
            row["name"]
            for row in await conn.execute_fetchall("PRAGMA table_info(loot_splits)")
        }
        if split_cols and "confirmed" not in split_cols:
            await conn.execute("ALTER TABLE loot_splits ADD COLUMN review_message_id INTEGER")
            await conn.execute(
                "ALTER TABLE loot_splits ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0"
            )
            await conn.commit()
            logger.info("Migration: added loot split confirmation columns")

        player_cols = {
            row["name"]
            for row in await conn.execute_fetchall("PRAGMA table_info(loot_players)")
        }
        if player_cols and "user_id" not in player_cols:
            await conn.execute("ALTER TABLE loot_players ADD COLUMN user_id INTEGER")
            await conn.commit()
            logger.info("Migration: added loot_players.user_id column")

        event_col_names = {
            row["name"] for row in await conn.execute_fetchall("PRAGMA table_info(events)")
        }
        if "loot_thread_id" not in event_col_names:
            await conn.execute("ALTER TABLE events ADD COLUMN loot_thread_id INTEGER")
            await conn.commit()
            logger.info("Migration: added events.loot_thread_id column")
        if "started" not in event_col_names:
            await conn.execute(
                "ALTER TABLE events ADD COLUMN started INTEGER NOT NULL DEFAULT 0"
            )
            # Raids already in the past are treated as started, so they aren't
            # suddenly auto-closed or given loot threads.
            now = datetime.now().astimezone()
            for row in await conn.execute_fetchall("SELECT id, start_time FROM events"):
                start = ensure_timezone_aware(datetime.fromisoformat(row["start_time"]))
                if start <= now:
                    await conn.execute("UPDATE events SET started = 1 WHERE id = ?", (row["id"],))
            await conn.commit()
            logger.info("Migration: added events.started column")

        settings_cols = {
            row["name"]
            for row in await conn.execute_fetchall("PRAGMA table_info(guild_settings)")
        }
        if settings_cols and "raid_channel_id" not in settings_cols:
            await conn.execute("ALTER TABLE guild_settings ADD COLUMN raid_channel_id INTEGER")
            await conn.commit()
            logger.info("Migration: added guild_settings.raid_channel_id column")

        event_cols = await conn.execute_fetchall("PRAGMA table_info(events)")
        thread_col = next((c for c in event_cols if c["name"] == "thread_id"), None)
        if thread_col is None or not thread_col["notnull"]:
            return

        await conn.commit()
        backup = self._path.with_suffix(self._path.suffix + ".pre-dropdown.bak")
        if not backup.exists():
            shutil.copy2(self._path, backup)
            logger.info("Migration: backed up database to %s", backup)

        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.executescript(
            """
            BEGIN;
            CREATE TABLE events_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL UNIQUE,
                thread_id INTEGER UNIQUE,
                creator_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                leaving_from TEXT NOT NULL,
                map_type TEXT NOT NULL,
                start_time TEXT NOT NULL,
                requirements TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                loot_thread_id INTEGER,
                started INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO events_new (
                id, guild_id, channel_id, message_id, thread_id, creator_id, title,
                leaving_from, map_type, start_time, requirements, closed, created_at,
                loot_thread_id, started
            )
            SELECT
                id, guild_id, channel_id, message_id, thread_id, creator_id, title,
                leaving_from, map_type, start_time, requirements, closed, created_at,
                loot_thread_id, started
            FROM events;
            DROP TABLE events;
            ALTER TABLE events_new RENAME TO events;
            COMMIT;
            """
        )
        await conn.execute("PRAGMA foreign_keys = ON")
        logger.info("Migration: events.thread_id is now optional (dropdown signups)")

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
        thread_id: int | None = None,
        creator_id: int,
        title: str,
        leaving_from: str,
        map_type: str,
        start_time: datetime,
        requirements: str,
        slots: list[tuple[int, str, str | None]],
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
        for slot_number, role_name, emoji in slots:
            await self.conn.execute(
                """
                INSERT INTO slots (event_id, slot_number, role_name, emoji)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, slot_number, role_name, emoji),
            )
            slot_models.append(
                Slot(slot_number=slot_number, role_name=role_name, emoji=emoji)
            )

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
            SELECT slot_number, role_name, user_id, emoji
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
                emoji=slot_row["emoji"],
            )
            for slot_row in slot_rows
        ]
        status_rows = await self.conn.execute_fetchall(
            "SELECT user_id, status FROM event_statuses WHERE event_id = ? ORDER BY updated_at",
            (row["id"],),
        )
        statuses = {r["user_id"]: r["status"] for r in status_rows}
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
            statuses=statuses,
            loot_thread_id=row["loot_thread_id"],
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

    async def get_events_by_guild(self, guild_id: int) -> list[FarmEvent]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE guild_id = ? ORDER BY id DESC",
            (guild_id,),
        )
        return [await self._row_to_event(row) for row in rows]

    # ── Signup statuses (tentative / absence) ─────────────────────────────

    async def set_user_status(self, event_id: int, user_id: int, status: str | None) -> None:
        if status is None:
            await self.conn.execute(
                "DELETE FROM event_statuses WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            )
        else:
            await self.conn.execute(
                """
                INSERT INTO event_statuses (event_id, user_id, status)
                VALUES (?, ?, ?)
                ON CONFLICT (event_id, user_id)
                DO UPDATE SET status = excluded.status, updated_at = datetime('now')
                """,
                (event_id, user_id, status),
            )
        await self.conn.commit()

    # ── Loot threads & screenshot splits ──────────────────────────────────

    async def get_loot_channel(self, guild_id: int) -> int | None:
        rows = await self.conn.execute_fetchall(
            "SELECT loot_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        return rows[0]["loot_channel_id"] if rows else None

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        if not rows:
            return GuildSettings(guild_id=guild_id)
        return GuildSettings(
            guild_id=guild_id,
            raid_channel_id=rows[0]["raid_channel_id"],
            loot_channel_id=rows[0]["loot_channel_id"],
        )

    async def update_guild_settings(
        self,
        guild_id: int,
        *,
        raid_channel_id: int | None = None,
        loot_channel_id: int | None = None,
    ) -> None:
        """Set the given channels (None leaves a setting unchanged)."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
        )
        if raid_channel_id is not None:
            await self.conn.execute(
                "UPDATE guild_settings SET raid_channel_id = ? WHERE guild_id = ?",
                (raid_channel_id, guild_id),
            )
        if loot_channel_id is not None:
            await self.conn.execute(
                "UPDATE guild_settings SET loot_channel_id = ? WHERE guild_id = ?",
                (loot_channel_id, guild_id),
            )
        await self.conn.commit()

    async def get_unstarted_events(self) -> list[FarmEvent]:
        """Raids whose start hasn't been processed yet (signups not auto-closed)."""
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE started = 0 ORDER BY id"
        )
        return [await self._row_to_event(row) for row in rows]

    async def set_started(self, event_id: int, started: bool) -> None:
        await self.conn.execute(
            "UPDATE events SET started = ? WHERE id = ?", (int(started), event_id)
        )
        await self.conn.commit()

    async def set_loot_thread(self, event_id: int, thread_id: int) -> None:
        await self.conn.execute(
            "UPDATE events SET loot_thread_id = ? WHERE id = ?", (thread_id, event_id)
        )
        await self.conn.commit()

    async def get_event_by_loot_thread(self, thread_id: int) -> FarmEvent | None:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM events WHERE loot_thread_id = ?", (thread_id,)
        )
        return await self._row_to_event(rows[0]) if rows else None

    async def get_loot_split(self, event_id: int) -> LootSplit:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM loot_splits WHERE event_id = ?", (event_id,)
        )
        players = [
            LootPlayer(name=r["name"], user_id=r["user_id"])
            for r in await self.conn.execute_fetchall(
                "SELECT name, user_id FROM loot_players WHERE event_id = ? ORDER BY rowid",
                (event_id,),
            )
        ]
        if not rows:
            return LootSplit(event_id=event_id, players=players)
        row = rows[0]
        return LootSplit(
            event_id=event_id,
            market_value=row["market_value"],
            market_value_approx=bool(row["market_value_approx"]),
            repair_cost=row["repair_cost"],
            silver_bags=row["silver_bags"],
            panel_message_id=row["panel_message_id"],
            review_message_id=row["review_message_id"],
            confirmed=bool(row["confirmed"]),
            players=players,
        )

    async def update_loot_split(self, event_id: int, **fields: Any) -> None:
        """Upsert columns of loot_splits (market_value, repair_cost, silver_bags, ...)."""
        allowed = {
            "market_value", "market_value_approx", "repair_cost", "silver_bags",
            "panel_message_id", "review_message_id", "confirmed",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown loot split fields: {unknown}")
        await self.conn.execute(
            "INSERT OR IGNORE INTO loot_splits (event_id) VALUES (?)", (event_id,)
        )
        if fields:
            set_clause = ", ".join(f"{col} = ?" for col in fields)
            await self.conn.execute(
                f"UPDATE loot_splits SET {set_clause} WHERE event_id = ?",
                [*fields.values(), event_id],
            )
        await self.conn.commit()

    async def add_loot_players(
        self, event_id: int, players: list[tuple[str, int | None]]
    ) -> list[LootPlayer]:
        """Add (name, matched user_id) pairs, skipping a name or signup already present.

        Returns the players newly added.
        """
        existing = await self.conn.execute_fetchall(
            "SELECT normalized, user_id FROM loot_players WHERE event_id = ?", (event_id,)
        )
        seen_names = {r["normalized"] for r in existing}
        seen_users = {r["user_id"] for r in existing if r["user_id"] is not None}
        added: list[LootPlayer] = []
        for name, user_id in players:
            clean = name.strip()
            if not clean or clean.lower() in seen_names:
                continue
            if user_id is not None and user_id in seen_users:
                continue
            await self.conn.execute(
                "INSERT INTO loot_players (event_id, normalized, name, user_id) VALUES (?, ?, ?, ?)",
                (event_id, clean.lower(), clean, user_id),
            )
            seen_names.add(clean.lower())
            if user_id is not None:
                seen_users.add(user_id)
            added.append(LootPlayer(name=clean, user_id=user_id))
        await self.conn.commit()
        return added

    async def link_loot_player(self, event_id: int, name: str, user_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE loot_players SET user_id = ? WHERE event_id = ? AND normalized = ?",
            (user_id, event_id, name.strip().lower()),
        )
        await self.conn.commit()

    async def replace_loot_players(
        self, event_id: int, players: list[tuple[str, int | None]]
    ) -> None:
        await self.conn.execute("DELETE FROM loot_players WHERE event_id = ?", (event_id,))
        await self.add_loot_players(event_id, players)

    # ── Raid presets ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_preset(row: aiosqlite.Row) -> RaidPreset:
        return RaidPreset(
            id=row["id"],
            guild_id=row["guild_id"],
            name=row["name"],
            title=row["title"],
            map_type=row["map_type"],
            roles=row["roles"],
            requirements=row["requirements"],
            creator_id=row["creator_id"],
        )

    async def get_presets(self, guild_id: int) -> list[RaidPreset]:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM raid_presets WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return [self._row_to_preset(row) for row in rows]

    async def get_preset(self, guild_id: int, name: str) -> RaidPreset | None:
        rows = await self.conn.execute_fetchall(
            "SELECT * FROM raid_presets WHERE guild_id = ? AND name = ?",
            (guild_id, name.strip()),
        )
        return self._row_to_preset(rows[0]) if rows else None

    async def save_preset(
        self,
        *,
        guild_id: int,
        name: str,
        title: str,
        map_type: str,
        roles: str,
        requirements: str,
        creator_id: int,
        original_name: str | None = None,
    ) -> None:
        """Create a preset, or update it (renaming from original_name if given)."""
        if original_name and original_name.strip().lower() != name.strip().lower():
            await self.conn.execute(
                "DELETE FROM raid_presets WHERE guild_id = ? AND name = ?",
                (guild_id, original_name.strip()),
            )
        await self.conn.execute(
            """
            INSERT INTO raid_presets (
                guild_id, name, title, map_type, roles, requirements, creator_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, name) DO UPDATE SET
                name = excluded.name,
                title = excluded.title,
                map_type = excluded.map_type,
                roles = excluded.roles,
                requirements = excluded.requirements
            """,
            (guild_id, name.strip(), title, map_type, roles, requirements, creator_id),
        )
        await self.conn.commit()

    async def delete_preset(self, guild_id: int, name: str) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM raid_presets WHERE guild_id = ? AND name = ?",
            (guild_id, name.strip()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

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
        await self.conn.execute("DELETE FROM event_statuses WHERE event_id = ?", (event_id,))
        await self.conn.execute("DELETE FROM loot_players WHERE event_id = ?", (event_id,))
        await self.conn.execute("DELETE FROM loot_splits WHERE event_id = ?", (event_id,))
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
