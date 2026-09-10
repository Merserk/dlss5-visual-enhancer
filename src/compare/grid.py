from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

LABEL_FONT_SIZE = 20
LABEL_PADDING = 8
GUTTER = 4
CANVAS_BACKGROUND = (32, 32, 32)
HEADER_BACKGROUND = (24, 24, 24)
HEADER_TEXT = (255, 255, 255)
ERROR_BACKGROUND = (48, 16, 16)
ERROR_TEXT = (255, 200, 200)


@dataclass(slots=True)
class GridCell:
    label: str
    image: Image.Image | None
    error: str | None = None


def _scaled_size(size: tuple[int, int], target_long_edge: int | None) -> tuple[int, int]:
    if not target_long_edge:
        return size
    width, height = size
    scale = target_long_edge / max(width, height)
    if scale >= 1.0:
        return size  # never upscale a tile past its own native resolution
    return max(1, round(width * scale)), max(1, round(height * scale))


def _draw_label(draw: ImageDraw.ImageDraw, box, text: str, font, bg, fg) -> None:
    draw.rectangle(box, fill=bg)
    x0, y0, x1, y1 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + max(0, ((x1 - x0) - text_w) // 2)
    ty = y0 + max(0, ((y1 - y0) - text_h) // 2) - bbox[1]
    draw.multiline_text((tx, ty), text, font=font, fill=fg, align="center")


def compose_grid(
    cells: list[list[GridCell]],
    row_labels: list[str],
    col_labels: list[str],
    target_long_edge: int | None = 1024,
    show_row_labels: bool = True,
    show_col_labels: bool = True,
) -> Image.Image:
    """Tile a 2D grid of already-rendered cells into one labeled image.

    `cells[row][col]` must line up with `row_labels`/`col_labels`. `target_long_edge`
    scales every tile's longer edge down to at most this many pixels (never
    upscales) — each cell is scaled independently, so an axis that changes output
    resolution (e.g. Upscaling factor) doesn't distort anything. Pass None for
    full resolution, which can produce a very large image for big grids.
    A cell with `image=None` renders as a labeled error tile instead of failing
    the whole grid.
    """
    if not cells or not cells[0]:
        raise ValueError("compose_grid needs at least one row and one column.")

    font = ImageFont.load_default(size=LABEL_FONT_SIZE)
    label_h = font.size + LABEL_PADDING * 2
    placeholder_dim = target_long_edge or 256

    scaled: list[list[tuple[Image.Image | None, tuple[int, int]]]] = []
    for row in cells:
        scaled_row = []
        for cell in row:
            if cell.image is None:
                scaled_row.append((None, (placeholder_dim, placeholder_dim)))
                continue
            size = _scaled_size(cell.image.size, target_long_edge)
            resized = (
                cell.image.resize(size, Image.Resampling.LANCZOS)
                if size != cell.image.size
                else cell.image
            )
            scaled_row.append((resized, size))
        scaled.append(scaled_row)

    n_rows, n_cols = len(scaled), len(scaled[0])
    col_widths = [max(scaled[r][c][1][0] for r in range(n_rows)) for c in range(n_cols)]
    row_heights = [max(scaled[r][c][1][1] for c in range(n_cols)) for r in range(n_rows)]

    dummy_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if show_col_labels:
        for c in range(n_cols):
            label_w = dummy_draw.textbbox((0, 0), col_labels[c], font=font)[2] + LABEL_PADDING * 2
            col_widths[c] = max(col_widths[c], label_w)
    row_label_w = 0
    if show_row_labels:
        widest = max((dummy_draw.textbbox((0, 0), label, font=font)[2] for label in row_labels), default=0)
        row_label_w = widest + LABEL_PADDING * 2
    col_label_h = label_h if show_col_labels else 0

    canvas_w = row_label_w + sum(col_widths) + GUTTER * (n_cols - 1)
    canvas_h = col_label_h + sum(row_heights) + GUTTER * (n_rows - 1)
    canvas = Image.new("RGB", (canvas_w, canvas_h), CANVAS_BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    if show_col_labels:
        x = row_label_w
        for c, width in enumerate(col_widths):
            _draw_label(draw, (x, 0, x + width, col_label_h), col_labels[c], font, HEADER_BACKGROUND, HEADER_TEXT)
            x += width + GUTTER

    y = col_label_h
    for r, height in enumerate(row_heights):
        x = 0
        if show_row_labels:
            _draw_label(draw, (0, y, row_label_w, y + height), row_labels[r], font, HEADER_BACKGROUND, HEADER_TEXT)
            x = row_label_w
        for c, width in enumerate(col_widths):
            image, size = scaled[r][c]
            box = (x, y, x + width, y + height)
            if image is None:
                cell = cells[r][c]
                message = f"Failed:\n{(cell.error or 'unknown error')[:80]}"
                _draw_label(draw, box, message, font, ERROR_BACKGROUND, ERROR_TEXT)
            else:
                paste_x = x + (width - size[0]) // 2
                paste_y = y + (height - size[1]) // 2
                canvas.paste(image, (paste_x, paste_y))
            x += width + GUTTER
        y += height + GUTTER

    return canvas
