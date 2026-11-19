"""Read Albion Online loot / repair / party / DPS-meter screenshots with free local OCR.

Uses RapidOCR (runs on this machine, no API key, no cost). The screenshot type is
decided from what the image shows — its on-screen text and layout — never the file name.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

ScreenshotKind = Literal["loot", "repair", "party", "dps", "other"]

_MAX_EDGE = 2560
_PERCENT = re.compile(r"^\d{1,3}(?:[.,]\d{1,2})?\s*%$")
_EXACT_SILVER = re.compile(r"^\D{0,2}(\d{1,3}(?:[,.\s]\d{3})+|\d{4,})\D{0,2}$")
_ABBREVIATED = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kmb])\b", re.I)
_NAME = re.compile(r"^[A-Za-z0-9]{3,16}$")
_UI_WORDS = {
    "bank", "actions", "pay", "sort", "stack", "equip", "tier", "category", "search",
    "move", "all", "repair", "loadout", "select", "kg", "ava", "party", "leave",
}


@dataclass(slots=True)
class ScreenshotReading:
    """What was read from one screenshot."""

    kind: ScreenshotKind
    market_value: int | None = None
    market_value_is_approximate: bool = False
    repair_cost: int | None = None
    players: list[str] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class _Text:
    text: str
    x: float  # left
    y: float  # vertical centre
    h: float  # height


class ScreenshotReaderError(RuntimeError):
    """Raised when a screenshot can't be read."""


def _silver_digits(text: str) -> int | None:
    match = _EXACT_SILVER.match(text.strip())
    if not match:
        return None
    return int(re.sub(r"\D", "", match.group(1)))


def _same_row(a: _Text, b: _Text) -> bool:
    return abs(a.y - b.y) <= max(a.h, b.h) * 0.7


def classify(texts: list[_Text]) -> ScreenshotReading:
    """Decide what kind of screenshot this is and pull out the values."""
    lowered = [t.text.lower() for t in texts]

    # Repair popup — checked first because a bank grid (with its own market value
    # footer) and player names are often visible behind it.
    cost_label = next(
        (t for t, low in zip(texts, lowered) if "total cost" in low), None
    )
    if cost_label is not None or any("repair all items" in low for low in lowered):
        cost = None
        if cost_label is not None:
            inline = re.sub(r"(?i).*total cost:?", "", cost_label.text)
            cost = _silver_digits(inline) if inline.strip() else None
            if cost is None:
                candidates = [
                    t for t in texts
                    if t is not cost_label and t.x > cost_label.x and _same_row(t, cost_label)
                    and _silver_digits(t.text) is not None
                ]
                if candidates:
                    cost = _silver_digits(min(candidates, key=lambda t: t.x).text)
        if cost is None:
            return ScreenshotReading("repair", note="couldn't read the Total Cost number")
        return ScreenshotReading("repair", repair_cost=cost)

    # DPS meter — rows of "NN.NN%" with a player name to the right.
    percents = [t for t in texts if _PERCENT.match(t.text.strip())]
    if len(percents) >= 2:
        names: list[str] = []
        for pct in sorted(percents, key=lambda t: t.y):
            row = [
                t for t in texts
                if t is not pct and t.x > pct.x and _same_row(t, pct)
                and _NAME.match(t.text.strip())
            ]
            if row:
                names.append(min(row, key=lambda t: t.x).text.strip())
        if names:
            return ScreenshotReading("dps", players=names)

    # Loot — the bank/chest "Est. Market Value".
    if any("market value" in low for low in lowered):
        explainer = next(
            (t for t, low in zip(texts, lowered) if "market value is estimated" in low), None
        )
        if explainer is not None:
            above = [
                t for t in texts
                if t.y < explainer.y and explainer.y - t.y < explainer.h * 4
                and _silver_digits(t.text) is not None
            ]
            if above:
                value = _silver_digits(max(above, key=lambda t: t.y).text)
                return ScreenshotReading("loot", market_value=value)
        footer = next(
            (t for t, low in zip(texts, lowered) if "market value:" in low), None
        )
        if footer is not None:
            tail = footer.text.split(":", 1)[1]
            match = _ABBREVIATED.search(tail)
            if match:
                number = float(match.group(1).replace(",", "."))
                unit = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2).lower()]
                return ScreenshotReading(
                    "loot",
                    market_value=round(number * unit),
                    market_value_is_approximate=True,
                    note="only the rounded footer was visible — hover the value for the exact number",
                )
            exact = _silver_digits(tail)
            if exact is not None:
                return ScreenshotReading("loot", market_value=exact)
        return ScreenshotReading("loot", note="couldn't read the market value")

    # Party frames — mostly bare player names (plus health/energy numbers), no sentences.
    names = []
    seen: set[str] = set()
    for t in sorted(texts, key=lambda t: (t.y, t.x)):
        word = t.text.strip()
        if (
            _NAME.match(word)
            and not word.isdigit()
            and word.lower() not in _UI_WORDS
            and word.lower() not in seen
        ):
            names.append(word)
            seen.add(word.lower())
    sentences = sum(1 for t in texts if len(t.text.split()) >= 3)
    if names and sentences <= len(names) // 2:
        return ScreenshotReading("party", players=names)

    return ScreenshotReading("other")


def _prepare_image(data: bytes) -> bytes:
    """Validate the image and downscale very large uploads (keeps OCR fast)."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ScreenshotReaderError("That file isn't an image I can open.") from exc
    if max(image.size) <= _MAX_EDGE and image.format in {"PNG", "JPEG"}:
        return data
    image = image.convert("RGB")
    image.thumbnail((_MAX_EDGE, _MAX_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ScreenshotReader:
    """Runs OCR off the event loop, one image at a time."""

    def __init__(self) -> None:
        from rapidocr import RapidOCR  # heavy import; models load once

        self._engine = RapidOCR()
        self._lock = asyncio.Lock()

    def _ocr(self, data: bytes) -> list[_Text]:
        result = self._engine(data)
        if result is None or result.txts is None:
            return []
        texts: list[_Text] = []
        for box, txt in zip(result.boxes, result.txts):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            texts.append(
                _Text(
                    text=str(txt),
                    x=float(min(xs)),
                    y=float(sum(ys) / len(ys)),
                    h=float(max(ys) - min(ys)) or 1.0,
                )
            )
        return texts

    async def read(self, data: bytes) -> ScreenshotReading:
        prepared = _prepare_image(data)
        async with self._lock:
            try:
                texts = await asyncio.to_thread(self._ocr, prepared)
            except Exception as exc:
                logger.exception("OCR failed")
                raise ScreenshotReaderError("Couldn't read that screenshot.") from exc
        reading = classify(texts)
        logger.info("Screenshot read: %s", reading)
        return reading
