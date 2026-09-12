#!/usr/bin/env python3
"""把 SVG 图标渲染成各标准尺寸的 PNG。

为什么同时提供 PNG：桌面环境对 SVG 有额外的解释空间（例如把图标当作"符号化
图标"处理时会把所有填充色替换成单色，图形就变成一个纯色方块）。PNG 没有这些
歧义，且各尺寸由我们自己渲染，观感可控。这是 Debian 图标包的常见做法。

用法：packaging/make-icons.py <源 SVG> <输出目录>
"""

from __future__ import annotations

import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(svg_path: Path, out_dir: Path) -> list[Path]:
    written = []
    for size in SIZES:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg_path), size, size)
        target_dir = out_dir / f"{size}x{size}" / "apps"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{svg_path.stem}.png"
        pixbuf.savev(str(target), "png", [], [])
        written.append(target)
    return written


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    svg_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    for target in render(svg_path, out_dir):
        print(f"icon: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
