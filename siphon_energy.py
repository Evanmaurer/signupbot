"""Parsing and balance helpers for siphoned energy exports."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from io import StringIO


@dataclass(slots=True)
class SiphonImportResult:
    """Parsed siphoned energy export totals."""

    rows: int
    balances: dict[str, int]
    display_names: dict[str, str]


def normalize_player_name(name: str) -> str:
    """Normalize an Albion player name for exact matching."""
    return name.strip().casefold()


def parse_siphon_export(text: str) -> SiphonImportResult:
    """Parse a TSV export with Date, Player, Reason, Amount columns."""
    reader = csv.DictReader(StringIO(text.strip()), delimiter="\t")
    required = {"Date", "Player", "Reason", "Amount"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise ValueError('Expected tab-separated columns: "Date", "Player", "Reason", "Amount".')

    balances: defaultdict[str, int] = defaultdict(int)
    display_names: dict[str, str] = {}
    rows = 0

    for row in reader:
        player = (row.get("Player") or "").strip()
        amount_text = (row.get("Amount") or "").replace(",", "").strip()
        if not player:
            continue
        try:
            amount = int(amount_text)
        except ValueError as exc:
            raise ValueError(f'Invalid amount "{amount_text}" for player "{player}".') from exc

        normalized = normalize_player_name(player)
        balances[normalized] += amount
        display_names.setdefault(normalized, player)
        rows += 1

    if rows == 0:
        raise ValueError("No siphoned energy rows found in that file.")

    return SiphonImportResult(
        rows=rows,
        balances=dict(balances),
        display_names=display_names,
    )
