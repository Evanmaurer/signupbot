"""Preset customisation helpers: stored raid images, embed colours and role icons."""

from __future__ import annotations

import io
import logging
import re
import shutil
import uuid
from pathlib import Path

import aiohttp
import discord
from PIL import Image

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_IMAGE_PATH = ROOT / "assets" / "raid_comp.png"
PRESET_IMAGE_DIR = ROOT / "preset_images"

# Raid posts attach their picture as "raid_comp.<ext>" so it can be found again
# when the post is re-rendered.
IMAGE_BASENAME = "raid_comp"
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024

EMOJI_SIZE = 128
MAX_EMOJI_BYTES = 256 * 1024
ICON_EMOJI_PREFIX = "rp"  # application emojis made by /ava preset icon

NAMED_COLOURS = {
    "green": 0x57F287,
    "red": 0xED4245,
    "blue": 0x3498DB,
    "blurple": 0x5865F2,
    "purple": 0x9B59B6,
    "pink": 0xEB459E,
    "gold": 0xF1C40F,
    "yellow": 0xFEE75C,
    "orange": 0xE67E22,
    "teal": 0x1ABC9C,
    "white": 0xFFFFFF,
    "black": 0x23272A,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
}
_HEX = re.compile(r"^(?:#|0x)?([0-9a-f]{6})$", re.I)


# ── colours ───────────────────────────────────────────────────────────────────


def parse_colour(text: str) -> int | None:
    """Parse '#ff8800', 'ff8800' or a colour name. Empty means default (None)."""
    text = text.strip().lower()
    if not text:
        return None
    if text in NAMED_COLOURS:
        return NAMED_COLOURS[text]
    match = _HEX.match(text)
    if match:
        return int(match.group(1), 16)
    raise ValueError(
        f'"{text}" isn\'t a colour — use a hex code like `#ff8800` or one of: '
        + ", ".join(sorted(set(NAMED_COLOURS) - {"gray"}))
    )


def format_colour(colour: int | None) -> str:
    return f"#{colour:06x}" if colour is not None else ""


# ── raid images ───────────────────────────────────────────────────────────────


def attachment_ext(attachment: discord.Attachment) -> str | None:
    """Return the image extension for an uploaded file, or None if it isn't an image."""
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if ext in IMAGE_EXTS:
        return ext
    content_type = (attachment.content_type or "").split(";")[0]
    if content_type.startswith("image/"):
        guess = content_type.split("/", 1)[1]
        if guess in IMAGE_EXTS:
            return guess
    return None


async def store_image(attachment: discord.Attachment) -> str:
    """Save an uploaded image for a preset and return its stored filename."""
    ext = attachment_ext(attachment)
    if ext is None:
        raise ValueError("The image must be a PNG, JPG, GIF or WEBP file.")
    if attachment.size > MAX_IMAGE_BYTES:
        raise ValueError("That image is too big — keep it under 8 MB.")
    data = await attachment.read()
    PRESET_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (PRESET_IMAGE_DIR / name).write_bytes(data)
    return name


def copy_image(stored_name: str | None) -> str | None:
    """Duplicate a preset's stored image (for copies) and return the new filename."""
    source = image_file(stored_name)
    if source is None:
        return None
    name = f"{uuid.uuid4().hex}{source.suffix}"
    shutil.copy2(source, PRESET_IMAGE_DIR / name)
    return name


def delete_image(stored_name: str | None) -> None:
    path = image_file(stored_name)
    if path is not None:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Couldn't delete preset image %s: %s", path, exc)


def image_file(stored_name: str | None) -> Path | None:
    """Path of a preset's stored image, if it still exists."""
    if not stored_name:
        return None
    path = PRESET_IMAGE_DIR / Path(stored_name).name
    return path if path.is_file() else None


def raid_image_for(stored_name: str | None) -> tuple[Path, str] | None:
    """(file to attach, attachment filename) for a raid post, falling back to the default."""
    path = image_file(stored_name)
    if path is None and DEFAULT_IMAGE_PATH.exists():
        path = DEFAULT_IMAGE_PATH
    if path is None:
        return None
    return path, f"{IMAGE_BASENAME}{path.suffix.lower()}"


def _embed_image_name(message: discord.Message) -> str | None:
    """Filename of a raid picture the post's embed is showing.

    Discord leaves attachments that an embed displays out of message.attachments,
    so the embed's image URL is the only place the filename shows up.
    """
    for embed in message.embeds:
        url = embed.image.url if embed.image else None
        if not url:
            continue
        name = url.split("?", 1)[0].rsplit("/", 1)[-1]
        if name.lower().startswith(f"{IMAGE_BASENAME}."):
            return name
    return None


def posted_image_name(message: discord.Message | None) -> str | None:
    """Filename of the raid picture on a posted raid message (shown or not)."""
    if message is None:
        return None
    for attachment in message.attachments:
        if attachment.filename.lower().startswith(f"{IMAGE_BASENAME}."):
            return attachment.filename
    return _embed_image_name(message)


class _EmbedImage:
    """Stand-in for a discord.Attachment, for a picture only reachable via the embed URL."""

    def __init__(self, url: str, filename: str) -> None:
        self.url = url
        self.filename = filename
        self.content_type: str | None = None
        self.size = 0

    async def read(self) -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.url) as response:
                response.raise_for_status()
                return await response.read()


def posted_image(message: discord.Message):  # -> discord.Attachment | _EmbedImage | None
    """The raid picture on a post, for copying it into a preset (anything with read())."""
    name = posted_image_name(message)
    if name is None:
        return None
    attachment = next((a for a in message.attachments if a.filename == name), None)
    if attachment is not None:
        return attachment
    for embed in message.embeds:
        if embed.image and embed.image.url:
            return _EmbedImage(embed.image.url, name)
    return None


# ── role icons ────────────────────────────────────────────────────────────────


def resolve_server_emojis(roles_text: str, guild: discord.Guild | None) -> str:
    """Turn a leading ':emoji_name:' on each role line into the server's custom emoji."""
    if guild is None:
        return roles_text
    by_name = {e.name.lower(): e for e in guild.emojis}

    def fix(line: str) -> str:
        match = re.match(r"^(\s*[-•]?\s*):(\w{2,32}):(\s+)", line)
        if not match:
            return line
        emoji = by_name.get(match.group(2).lower())
        if emoji is None:
            return line
        return f"{match.group(1)}{emoji}{match.group(3)}{line[match.end():]}"

    return "\n".join(fix(line) for line in roles_text.splitlines())


def icon_emoji_name(preset_id: int, role_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", role_name.lower()).strip("_") or "role"
    return f"{ICON_EMOJI_PREFIX}{preset_id}_{slug}"[:32]


async def icon_image_bytes(attachment: discord.Attachment) -> bytes:
    """Read an uploaded role icon and shrink it to emoji size."""
    ext = attachment_ext(attachment)
    if ext is None:
        raise ValueError("The icon must be a PNG, JPG, GIF or WEBP image.")
    if attachment.size > MAX_IMAGE_BYTES:
        raise ValueError("That image is too big — keep it under 8 MB.")
    data = await attachment.read()
    if ext == "gif" and len(data) <= MAX_EMOJI_BYTES:
        return data  # keep animation
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGBA")
            img.thumbnail((EMOJI_SIZE, EMOJI_SIZE))
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception as exc:  # Pillow raises many types for bad files
        raise ValueError("Couldn't read that image — try a PNG or JPG.") from exc
