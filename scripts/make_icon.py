"""生成桌面 EXE 和托盘用的图标。只依赖 Pillow。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "desktop" / "assets" / "icon.ico"


def draw(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_context = ImageDraw.Draw(image)
    margin = max(1, size // 16)
    radius = max(2, size // 6)
    draw_context.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill=(11, 15, 20, 255),
    )
    wick_left = int(size * 0.47)
    wick_right = int(size * 0.53)
    body_left = int(size * 0.38)
    body_right = int(size * 0.62)
    draw_context.rectangle((wick_left, int(size * 0.18), wick_right, int(size * 0.82)), fill=(226, 61, 61, 255))
    draw_context.rectangle((body_left, int(size * 0.32), body_right, int(size * 0.70)), fill=(226, 61, 61, 255))
    return image


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    draw(256).save(OUTPUT, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    print(OUTPUT)


if __name__ == "__main__":
    main()
