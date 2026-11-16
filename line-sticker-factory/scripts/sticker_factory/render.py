"""Deterministic static sticker rendering. Never performs generation or auto-approval."""
from __future__ import annotations

from collections import deque
import hashlib
import io
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
    return value


def _color(value):
    try:
        return ImageColor.getrgb(value)[:3]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f'Invalid color: {value!r}') from exc


def validate_font(font_path: Path | None, text: str, font_index: int = 0) -> str | None:
    """Require real glyph coverage; return file/face fingerprint, never a fallback."""
    if not text:
        return None
    if font_path is None or not Path(font_path).is_file():
        raise ValueError('Caption requires an existing font file')
    _integer(font_index, 'font_index', 0, 255)
    path = Path(font_path)
    try:
        from fontTools.ttLib import TTFont
        with path.open('rb') as handle:
            is_collection = handle.read(4) == b'ttcf'
        if not is_collection and font_index != 0:
            raise ValueError('Non-collection font requires face index 0')
        with TTFont(str(path), fontNumber=font_index, lazy=True) as font:
            coverage = font.getBestCmap() or {}
            missing = sorted({ord(c) for c in text if c not in '\n\r\t ' and ord(c) not in coverage})
        if missing:
            raise ValueError('Font lacks glyphs: ' + ', '.join(f'U+{c:04X}' for c in missing))
        ImageFont.truetype(str(path), 16, index=font_index)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'Cannot read font face {font_index}: {exc}') from exc
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    digest.update(f'\nface:{font_index}'.encode('ascii'))
    return digest.hexdigest()


def _load_source(path: Path):
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('Source exceeds 20 MiB')
        with Image.open(path) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValueError('Source exceeds 16 million pixels')
            if getattr(source, 'n_frames', 1) != 1:
                raise ValueError('Animated inputs are not supported by the static renderer')
            source.load()
            return ImageOps.exif_transpose(source).convert('RGBA')
    except (OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValueError(f'Cannot decode source image: {exc}') from exc


def _remove_edge_color(image, color, tolerance):
    """Only remove matching pixels connected to an edge, preserving enclosed colors."""
    width, height = image.size
    pixels = image.load()
    visited = bytearray(width * height)
    queue = deque()

    def enqueue(x, y):
        index = y * width + x
        if visited[index]:
            return
        visited[index] = 1
        pixel = pixels[x, y]
        if pixel[3] == 0 or max(abs(pixel[i] - color[i]) for i in range(3)) <= tolerance:
            queue.append(index)

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)
    while queue:
        index = queue.popleft()
        x, y = index % width, index // width
        r, g, b, _ = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
        if x:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)
    return image


def _fit(subject, width, height):
    canvas = Image.new('RGBA', (width, height))
    # Enlarge only as a delivery transformation; never claim extra source detail.
    scale = min(width / subject.width, height / subject.height)
    resized = subject.resize((max(1, int(subject.width * scale)), max(1, int(subject.height * scale))), Image.Resampling.LANCZOS)
    canvas.alpha_composite(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return canvas


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, 'PNG', dpi=(72, 72), optimize=True)
    return buffer.getvalue()


def _caption_layout(text, font_path, font_index, layer, width, height):
    max_size = _integer(layer.get('fontSize', 48), 'fontSize', 1, 256)
    min_size = _integer(layer.get('minFontSize', 12), 'minFontSize', 1, max_size)
    stroke = _integer(layer.get('strokeWidth', 2), 'strokeWidth', 0, 12)
    draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    spacing = 4
    for size in range(max_size, min_size - 1, -1):
        font = ImageFont.truetype(str(font_path), size, index=font_index)
        lines = []
        for paragraph in text.split('\n'):
            current = ''
            for char in paragraph:
                trial = current + char
                bbox = draw.textbbox((0, 0), trial, font=font, stroke_width=stroke)
                if bbox[2] - bbox[0] > width:
                    if not current:
                        break
                    lines.append(current)
                    current = char
                else:
                    current = trial
            else:
                lines.append(current)
                continue
            break
        else:
            boxes = [draw.textbbox((0, 0), line or ' ', font=font, stroke_width=stroke) for line in lines]
            total_height = sum(max(1, b[3] - b[1]) for b in boxes) + spacing * (len(lines) - 1)
            if total_height <= height and all(b[2] - b[0] <= width for b in boxes):
                return font, lines, boxes, total_height, spacing, stroke
    raise ValueError('Caption cannot fit the safe area at the minimum font size')


def render_slot(source_path: Path, recipe: dict, font_path: Path | None = None, font_index: int = 0) -> dict:
    """Render original source plus an explicit recipe. Callers must visually review output.

    crop: {x,y,width,height}; background: {mode:preserve-alpha|edge-color,color,tolerance};
    target: {width,height,margin}; captionLayer: {text,fontSize,minFontSize,maxHeight,
    color,strokeColor,strokeWidth,enabled,position:top|bottom}; border: {width,color}.
    """
    if not isinstance(recipe, dict):
        raise ValueError('Recipe must be an object')
    image = _load_source(Path(source_path))
    warnings = []
    crop = recipe.get('crop')
    if crop is not None:
        if not isinstance(crop, dict):
            raise ValueError('crop must be an object')
        x = _integer(crop.get('x'), 'crop.x', 0, image.width - 1)
        y = _integer(crop.get('y'), 'crop.y', 0, image.height - 1)
        width = _integer(crop.get('width'), 'crop.width', 1, image.width - x)
        height = _integer(crop.get('height'), 'crop.height', 1, image.height - y)
        image = image.crop((x, y, x + width, y + height))
    background = recipe.get('background') or {'mode': 'preserve-alpha'}
    if not isinstance(background, dict):
        raise ValueError('background must be an object')
    mode = background.get('mode', 'preserve-alpha')
    if mode == 'edge-color':
        if 'color' not in background:
            raise ValueError('edge-color requires an explicit background color')
        tolerance = _integer(background.get('tolerance', 0), 'tolerance', 0, 255)
        image = _remove_edge_color(image, _color(background['color']), tolerance)
        warnings.append('Edge-color removal requires visual checks for lost subject pixels')
    elif mode != 'preserve-alpha':
        raise ValueError('Unsupported background mode')
    elif image.getchannel('A').getextrema()[0] == 255:
        warnings.append('Source has no transparent region; background has not been removed')
    bounds = image.getchannel('A').getbbox()
    if bounds is None:
        raise ValueError('Source is fully transparent after processing')
    subject = image.crop(bounds)
    border = recipe.get('border') or {}
    if not isinstance(border, dict):
        raise ValueError('border must be an object')
    border_width = _integer(border.get('width', 0), 'border.width', 0, 20)
    if border_width:
        subject = ImageOps.expand(subject, border=border_width, fill=(0, 0, 0, 0))
        halo = Image.new('RGBA', subject.size, (*_color(border.get('color', '#ffffff')), 0))
        halo.putalpha(subject.getchannel('A').filter(ImageFilter.MaxFilter(border_width * 2 + 1)))
        halo.alpha_composite(subject)
        subject = halo
    target = recipe.get('target') or {}
    if not isinstance(target, dict):
        raise ValueError('target must be an object')
    width = _integer(target.get('width', 370), 'target.width', 2, 370)
    height = _integer(target.get('height', 320), 'target.height', 2, 320)
    if width % 2 or height % 2:
        raise ValueError('Target dimensions must be even')
    margin = _integer(target.get('margin', 10), 'target.margin', 0, min(width, height) // 2 - 1)
    safe_width, safe_height = width - margin * 2, height - margin * 2
    base = Image.new('RGBA', (width, height))
    base.alpha_composite(_fit(subject, safe_width, safe_height), (margin, margin))
    rendered = base.copy()
    layer = recipe.get('captionLayer') or {}
    if not isinstance(layer, dict):
        raise ValueError('captionLayer must be an object')
    enabled = layer.get('enabled', True)
    position = layer.get('position', 'bottom')
    if type(enabled) is not bool:
        raise ValueError('caption.enabled must be a boolean')
    if position not in ('top', 'bottom'):
        raise ValueError('caption.position must be top or bottom')
    raw_text = layer.get('text', '')
    if not isinstance(raw_text, str) or len(raw_text) > 500:
        raise ValueError('Caption must be text of at most 500 characters')
    text = raw_text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ').strip()
    if not enabled:
        text = ''
    font_hash = validate_font(font_path, text, font_index)
    if text:
        caption_height = _integer(layer.get('maxHeight', min(96, safe_height // 2)), 'caption.maxHeight', 1, safe_height - 1)
        font, lines, boxes, total_height, spacing, stroke = _caption_layout(text, font_path, font_index, layer, safe_width, caption_height)
        rendered = Image.new('RGBA', (width, height))
        art_y = margin + caption_height if position == 'top' else margin
        rendered.alpha_composite(_fit(subject, safe_width, safe_height - caption_height), (margin, art_y))
        draw = ImageDraw.Draw(rendered)
        caption_y = margin if position == 'top' else height - margin - caption_height
        y = caption_y + (caption_height - total_height) // 2
        fill, outline = _color(layer.get('color', '#222222')), _color(layer.get('strokeColor', '#ffffff'))
        for line, bounds in zip(lines, boxes):
            x = margin + (safe_width - (bounds[2] - bounds[0])) // 2
            draw.text((x - bounds[0], y - bounds[1]), line, font=font, fill=(*fill, 255), stroke_width=stroke, stroke_fill=(*outline, 255))
            y += max(1, bounds[3] - bounds[1]) + spacing
    base_png, render_png = _png(base), _png(rendered)
    return {'base_png': base_png, 'render_png': render_png,
            'render_hash': hashlib.sha256(render_png).hexdigest(), 'font_hash': font_hash,
            'warnings': warnings, 'requires_visual_review': True}
