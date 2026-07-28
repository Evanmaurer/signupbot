"""Parsing and balance helpers for siphoned energy exports."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from io import StringIO

# Ignore specific remove rows that are known bank/admin adjustments.
# Matches player name (case-insensitive), including names like "Name/Alt".
IGNORE_EXACT_50_REMOVE_PLAYERS = frozenset(
    {
        "jdlikesmilfs",
        "thesebigoldnuts",
        "deezbigolnuts",  # common spelling of the same player
    }
)
IGNORE_OVER_300_REMOVE_PLAYERS = frozenset(
    {
        "obscureotter",
        "jdlikesmilfs",
        "kswan",
    }
)


@dataclass(slots=True)
class SiphonImportResult:
    """Parsed siphoned energy export totals."""

    rows: int
    duplicate_rows: int
    ignored_rows: int
    balances: dict[str, int]
    display_names: dict[str, str]


def normalize_player_name(name: str) -> str:
    """Normalize an Albion player name for exact matching."""
    return name.strip().casefold()


def _player_match_keys(normalized: str) -> set[str]:
    """Return match keys for a player, including the part before '/'."""
    keys = {normalized}
    if "/" in normalized:
        keys.add(normalized.split("/", 1)[0].strip())
    return {key for key in keys if key}


def _is_remove_row(reason: str, amount: int) -> bool:
    """Return True when a row represents a siphon remove/withdrawal."""
    if amount < 0:
        return True
    return "remove" in reason.casefold()


def should_ignore_siphon_row(player: str, reason: str, amount: int) -> bool:
    """Return True if this remove row should be excluded from balances."""
    if not _is_remove_row(reason, amount):
        return False

    magnitude = abs(amount)
    keys = _player_match_keys(normalize_player_name(player))

    if magnitude == 50 and keys & IGNORE_EXACT_50_REMOVE_PLAYERS:
        return True
    if magnitude > 300 and keys & IGNORE_OVER_300_REMOVE_PLAYERS:
        return True
    return False


def parse_siphon_export(text: str) -> SiphonImportResult:
    """Parse a TSV export with Date, Player, Reason, Amount columns."""
    reader = csv.DictReader(StringIO(text.strip()), delimiter="\t")
    required = {"Date", "Player", "Reason", "Amount"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise ValueError('Expected tab-separated columns: "Date", "Player", "Reason", "Amount".')

    balances: defaultdict[str, int] = defaultdict(int)
    display_names: dict[str, str] = {}
    seen_rows: set[tuple[str, str, str, int]] = set()
    rows = 0
    duplicate_rows = 0
    ignored_rows = 0

    for row in reader:
        date_text = (row.get("Date") or "").strip()
        player = (row.get("Player") or "").strip()
        reason = (row.get("Reason") or "").strip()
        amount_text = (row.get("Amount") or "").replace(",", "").strip()
        if not player:
            continue
        try:
            amount = int(amount_text)
        except ValueError as exc:
            raise ValueError(f'Invalid amount "{amount_text}" for player "{player}".') from exc

        normalized = normalize_player_name(player)
        duplicate_key = (date_text, normalized, reason, amount)
        if duplicate_key in seen_rows:
            duplicate_rows += 1
            continue
        seen_rows.add(duplicate_key)

        if should_ignore_siphon_row(player, reason, amount):
            ignored_rows += 1
            continue

        balances[normalized] += amount
        display_names.setdefault(normalized, player)
        rows += 1

    if rows == 0 and ignored_rows == 0:
        raise ValueError("No siphoned energy rows found in that file.")

    return SiphonImportResult(
        rows=rows,
        duplicate_rows=duplicate_rows,
        ignored_rows=ignored_rows,
        balances=dict(balances),
        display_names=display_names,
    )
