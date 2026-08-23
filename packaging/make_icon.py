"""Generate the Windows application icon.

An unsigned executable with the default PyInstaller icon looks like something a
stranger emailed you. A real icon is the cheapest part of not looking that way,
so it is generated here rather than pasted in as an opaque binary: the shape is
readable and reviewable, and `packaging/dotameta.ico` can be rebuilt at any time.

    python packaging/make_icon.py

Drawn at 4x and downsampled, because Windows renders this at 16px in the taskbar
and a hairline edge turns to mush there. Nothing here is a Valve asset.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 4
SIZE = 256 * SCALE
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

TILE_TOP = (31, 28, 27)
TILE_BOTTOM = (20, 18, 17)
BORDER = (58, 51, 48)
ARROW = (97, 214, 154)
BAR = (122, 111, 102)

OUTPUT = Path(__file__).with_name("dotameta.ico")


def tile() -> Image.Image:
    """A rounded square with a vertical gradient, like an app tile."""
    gradient = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        weight = y / (SIZE - 1)
        channels = zip(TILE_TOP, TILE_BOTTOM, strict=True)
        gradient.putpixel(
            (0, y),
            tuple(round(top + (bottom - top) * weight) for top, bottom in channels),
        )
    gradient = gradient.resize((SIZE, SIZE))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, SIZE - 1, SIZE - 1), radius=int(SIZE * 0.22), fill=255
    )
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    image.paste(gradient, (0, 0), mask)

    edge = ImageDraw.Draw(image)
    edge.rounded_rectangle(
        (0, 0, SIZE - 1, SIZE - 1),
        radius=int(SIZE * 0.22),
        outline=BORDER + (255,),
        width=max(1, int(SIZE * 0.012)),
    )
    return image


def climb(image: Image.Image) -> None:
    """Two flat bars and one that became an arrow: a record, and the climb.

    Everything is thick and separated on purpose. At 16 pixels a bar is three
    pixels wide, so thin shapes and small gaps disappear into one smear.
    """
    draw = ImageDraw.Draw(image)
    unit = SIZE / 256
    for left, right, top in ((46, 88, 168), (104, 146, 140)):
        draw.rounded_rectangle(
            (left * unit, top * unit, right * unit, 210 * unit),
            radius=9 * unit,
            fill=BAR + (255,),
        )
    draw.polygon(
        [(183 * unit, 46 * unit), (232 * unit, 106 * unit), (134 * unit, 106 * unit)],
        fill=ARROW + (255,),
    )
    draw.rounded_rectangle(
        (162 * unit, 100 * unit, 204 * unit, 210 * unit),
        radius=9 * unit,
        fill=ARROW + (255,),
    )


def main() -> None:
    image = tile()
    climb(image)
    image = image.resize((256, 256), Image.LANCZOS)
    image.save(OUTPUT, sizes=ICO_SIZES)
    image.save(OUTPUT.with_suffix(".png"))  # preview, not shipped
    print(f"wrote {OUTPUT} with sizes {[size[0] for size in ICO_SIZES]}")


if __name__ == "__main__":
    main()
