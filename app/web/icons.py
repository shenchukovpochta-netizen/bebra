"""Значки панели для главного экрана телефона: python -m app.web.icons

Рисует PNG в app/web/static из «молнии» брендбука (templates/_bolt.html):
оранжевая молния на тёмном. Слово-логотип на значок не идёт - на
шестидесяти пикселях его не прочесть, а подпись под значком телефон
пишет сам из манифеста. Файлы лежат в проекте готовыми: панель их только
отдаёт, а этот модуль нужен, когда меняется бренд.

SVG разбирается здесь же, без cairo: в контуре молнии только M, L и C,
и тащить библиотеку ради одной фигуры незачем.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BOLT = HERE / "templates" / "_bolt.html"
STATIC = HERE / "static"
INK = "#211F1D"
BRAND = "#FF5907"
# Во сколько раз крупнее рисуем перед уменьшением: сглаживание краёв
# без отдельной библиотеки.
SCALE = 4
# имя файла, сторона, высота молнии от стороны, скругление фона от стороны.
# Обычный значок - скруглённый квадрат: его показывает браузер как есть.
# maskable - квадрат во весь край, молния внутри безопасного круга (80 %
# стороны): Android сам вырезает круг или «каплю». Значок iPhone - тоже
# во весь край: углы скругляет iOS, а прозрачное она заливает чёрным.
ICONS = (("icon-192.png", 192, 0.64, 0.22),
         ("icon-512.png", 512, 0.64, 0.22),
         ("icon-maskable-512.png", 512, 0.56, 0.0),
         ("apple-touch-icon.png", 180, 0.64, 0.0))


def bolt_path(source: str) -> str:
    return re.search(r'<path[^>]*\sd="([^"]+)"', source).group(1)


def polygon(path: str, steps: int = 24) -> list[tuple[float, float]]:
    """Контур SVG в ломаную: кривые Безье - отрезками. Понимает только
    абсолютные M, L, C и Z - другое в молнии не встречается, и лучше
    упасть, чем нарисовать не ту фигуру."""
    tokens = re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", path)
    points: list[tuple[float, float]] = []
    cmd, i = "", 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                continue
        if cmd not in "MLC":
            raise ValueError(f"команда контура {cmd!r} не поддерживается")
        count = 6 if cmd == "C" else 2
        nums = [float(t) for t in tokens[i:i + count]]
        i += count
        if cmd == "C":
            (x0, y0), (x1, y1, x2, y2, x3, y3) = points[-1], nums
            for k in range(1, steps + 1):
                t = k / steps
                u = 1 - t
                points.append((u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                               u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
        else:
            points.append((nums[0], nums[1]))
    return points


def draw(size: int, bolt_share: float, radius_share: float,
         shape: list[tuple[float, float]]) -> Image.Image:
    """Один значок: фон цвета шапки панели, молния по центру."""
    big = size * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    if radius_share:
        pen.rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * radius_share),
                              fill=INK)
    else:
        pen.rectangle((0, 0, big, big), fill=INK)
    xs, ys = [x for x, _ in shape], [y for _, y in shape]
    k = big * bolt_share / (max(ys) - min(ys))
    dx = (big - (max(xs) - min(xs)) * k) / 2 - min(xs) * k
    dy = (big - (max(ys) - min(ys)) * k) / 2 - min(ys) * k
    pen.polygon([(x * k + dx, y * k + dy) for x, y in shape], fill=BRAND)
    image = image.resize((size, size), Image.LANCZOS)
    # Значок во весь край - непрозрачный: iOS залила бы прозрачное чёрным.
    return image if radius_share else image.convert("RGB")


def render(folder: Path = STATIC) -> list[Path]:
    shape = polygon(bolt_path(BOLT.read_text(encoding="utf-8")))
    written = []
    for name, size, bolt_share, radius_share in ICONS:
        target = folder / name
        draw(size, bolt_share, radius_share, shape).save(target, optimize=True)
        written.append(target)
    return written


if __name__ == "__main__":
    for path in render(Path(sys.argv[1]) if len(sys.argv) > 1 else STATIC):
        print(path)
