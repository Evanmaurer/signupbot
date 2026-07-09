"""Discord UI components for farm signup events."""

from __future__ import annotations

import discord


class FarmEventSelect(discord.ui.Select):
    """Select menu for choosing an active farm event when not in a thread."""

    def __init__(self, events: list[tuple[int, str]]) -> None:
        options = [
            discord.SelectOption(label=title[:100], value=str(event_id))
            for event_id, title in events[:25]
        ]
        super().__init__(
            placeholder="Select a farm event…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.selected_event_id: int | None = None

    async def callback(self, interaction: discord.Interaction) -> None:
        self.selected_event_id = int(self.values[0])
        self.view.stop()  # type: ignore[attr-defined]
        await interaction.response.defer()


class FarmEventSelectView(discord.ui.View):
    """View wrapping the farm event select menu."""

    def __init__(self, events: list[tuple[int, str]]) -> None:
        super().__init__(timeout=60)
        self.select = FarmEventSelect(events)
        self.add_item(self.select)

    @property
    def event_id(self) -> int | None:
        return self.select.selected_event_id
