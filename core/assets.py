"""Generate and manage golden branded assets for the GUI."""

import os
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

ASSETS_FOLDER = Path("assets")
ICON_SIZE = (128, 128)
BACKGROUND = "#0F0F0F"
GOLD = "#D4AF37"
GOLD_ALT = "#FFD700"


def _text_size(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, text: str) -> Tuple[int, int]:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        return draw.textsize(text, font=font)


def _create_icon(filename: str, symbol: str) -> None:
    icon_path = ASSETS_FOLDER / filename
    if icon_path.exists():
        return
    image = Image.new("RGBA", ICON_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("seguisym.ttf", 72)
    except OSError:
        font = ImageFont.load_default()
    text_width, text_height = _text_size(draw, font, symbol)
    draw.text(
        ((ICON_SIZE[0] - text_width) / 2, (ICON_SIZE[1] - text_height) / 2),
        symbol,
        font=font,
        fill=GOLD,
    )
    draw.rounded_rectangle([8, 8, 120, 120], radius=24, outline=GOLD_ALT, width=4)
    image.save(icon_path, optimize=True)


def ensure_assets() -> None:
    """Create the asset folder and generate icon assets if missing."""
    ASSETS_FOLDER.mkdir(exist_ok=True)
    _create_icon("logo.png", "L")
    _create_icon("scan.png", "S")
    _create_icon("report.png", "R")
    _create_icon("settings.png", "⚙")
