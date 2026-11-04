#!/usr/bin/env python3
"""Build a reusable static HTML diary from dated text files and photos."""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    print("Python 3.11 or newer is required.", file=sys.stderr)
    raise SystemExit(2)

try:
    from PIL import Image, ImageOps
except ModuleNotFoundError:
    print("Pillow is required. Run: python -m pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(2)


DATE_FILE_RE = re.compile(r"^(\d{8})\.txt$")
PHOTO_LINE_RE = re.compile(r"^#\s*(.*?)\s*$")
SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PLACEHOLDER_MARKER = "ここに"


@dataclass(frozen=True)
class Entry:
    key: str
    source: Path
    title: str
    blocks: list[tuple[str, str | list[str]]]

    @property
    def output_name(self) -> str:
        return f"{self.key}.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="日記テキストと写真から公開用HTMLを生成します。")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent,
                        help="入力フォルダ（既定: このスクリプトのフォルダ）")
    parser.add_argument("--config", default="diary.toml", help="設定ファイル名")
    parser.add_argument("--clean", action="store_true",
                        help="出力フォルダを削除してから生成（入力フォルダとは別の場合のみ）")
    return parser.parse_args()


def discover_diaries(root: Path) -> list[Path]:
    diaries = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        match = DATE_FILE_RE.fullmatch(path.name)
        if not match:
            continue
        try:
            datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError as exc:
            raise ValueError(f"Invalid diary date in filename: {path.name}") from exc
        diaries.append(path)
    return sorted(diaries, key=lambda p: p.name)


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def initialize_or_extend_config(config_path: Path, diary_paths: list[Path]) -> None:
    keys = [p.stem for p in diary_paths]
    if not config_path.exists():
        lines = [
            '# 日記サイトの設定。"ここに..."の文章を公開前に書き換えてください。',
            'site_title = "ここに日記全体のタイトルを入力してください"',
            'site_description = "ここに日記全体の概要を入力してください。"',
            'parent_index_url = "../essays.html"',
            'parent_index_label = "戻る"',
            'photo_directory = "photo"',
            'output_directory = "public"',
            'thumbnail_directory = "miniphoto"',
            'thumbnail_max_size = 320',
            '',
            '[entry_descriptions]',
        ]
        for key in keys:
            label = format_date_key(key)
            lines.append(f'{toml_string(key)} = {toml_string(f"ここに{label}の日記の説明を入力してください。")}')
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Created configuration: {config_path.name}")
        return

    config = load_config(config_path)
    descriptions = config.get("entry_descriptions", {})
    missing = [key for key in keys if key not in descriptions]
    if missing:
        with config_path.open("a", encoding="utf-8") as stream:
            stream.write("\n# Newly detected diary entries\n")
            for key in missing:
                label = format_date_key(key)
                stream.write(f'{toml_string(key)} = {toml_string(f"ここに{label}の日記の説明を入力してください。")}\n')
        print("Added placeholder descriptions for: " + ", ".join(missing))


def load_config(path: Path) -> dict:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def validate_config(config: dict) -> None:
    required = {
        "site_title": str,
        "site_description": str,
        "parent_index_url": str,
        "parent_index_label": str,
        "photo_directory": str,
        "output_directory": str,
        "thumbnail_directory": str,
        "thumbnail_max_size": int,
        "entry_descriptions": dict,
    }
    for name, expected in required.items():
        if name not in config:
            raise ValueError(f"Missing setting: {name}")
        if not isinstance(config[name], expected):
            raise ValueError(f"Setting {name} must be {expected.__name__}")
    if config["thumbnail_max_size"] < 1:
        raise ValueError("thumbnail_max_size must be positive")


def format_date_key(key: str) -> str:
    value = datetime.strptime(key, "%Y%m%d")
    return f"{value.year}年{value.month}月{value.day}日"


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"Cannot decode {path.name}; use UTF-8 or CP932")


def parse_entry(path: Path) -> Entry:
    lines = read_text(path).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError(f"Diary is empty: {path.name}")
    title = lines.pop(0).strip()
    blocks: list[tuple[str, str | list[str]]] = []
    for line_number, line in enumerate(lines, start=2):
        stripped = line.strip()
        if not stripped:
            continue
        photo_match = PHOTO_LINE_RE.fullmatch(stripped)
        if photo_match:
            symbols = photo_match.group(1).split()
            if not symbols:
                raise ValueError(f"{path.name}:{line_number}: # line has no image symbols")
            blocks.append(("photos", symbols))
        else:
            blocks.append(("text", line.strip()))
    return Entry(path.stem, path, title, blocks)


def image_files(photo_dir: Path) -> list[Path]:
    if not photo_dir.is_dir():
        raise ValueError(f"Photo directory not found: {photo_dir}")
    return sorted(
        (p for p in photo_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES),
        key=lambda p: p.name.casefold(),
    )


def match_photo(symbol: str, candidates: list[Path]) -> Path:
    needle = symbol.casefold()
    matches = [p for p in candidates if p.stem.casefold().endswith(needle)]
    if not matches:
        raise ValueError(f"No image matches symbol {symbol!r}")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise ValueError(f"Image symbol {symbol!r} is ambiguous: {names}")
    return matches[0]


def referenced_photos(entries: Iterable[Entry], candidates: list[Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for entry in entries:
        for kind, value in entry.blocks:
            if kind != "photos":
                continue
            assert isinstance(value, list)
            for symbol in value:
                result.setdefault(symbol, match_photo(symbol, candidates))
    return result


def resize_image(source: Path, destination: Path, max_size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original)
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        save_args = {}
        if destination.suffix.lower() in {".jpg", ".jpeg"}:
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            save_args = {"quality": 85, "optimize": True}
        image.save(destination, **save_args)


def page_shell(title: str, body: str, css_href: str = "style.css") -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="{html.escape(css_href, quote=True)}">
</head>
<body>
{body}
</body>
</html>
"""


def render_entry(entry: Entry, previous: Entry | None, following: Entry | None,
                 photo_map: dict[str, Path], thumbnail_dir: str) -> str:
    parts = ["<main>", f"  <h1>{html.escape(entry.title)}</h1>", '  <article class="diary-entry">']
    for kind, value in entry.blocks:
        if kind == "text":
            assert isinstance(value, str)
            parts.append(f"    <p>{html.escape(value)}</p>")
        else:
            assert isinstance(value, list)
            parts.append('    <div class="photo-row">')
            for symbol in value:
                photo = photo_map[symbol]
                src = f"{thumbnail_dir}/{photo.name}"
                parts.append(
                    f'      <img src="{html.escape(src, quote=True)}" '
                    f'alt="写真 {html.escape(symbol, quote=True)}" loading="lazy">'
                )
            parts.append("    </div>")
    parts.extend(["  </article>", '  <nav class="entry-navigation" aria-label="日記内の移動">'])
    if previous:
        parts.append(f'    <a rel="prev" href="{previous.output_name}">前の日へ</a>')
    parts.append('    <a href="index.html">目次へ</a>')
    if following:
        parts.append(f'    <a rel="next" href="{following.output_name}">次の日へ</a>')
    parts.extend(["  </nav>", "</main>"])
    return page_shell(entry.title, "\n".join(parts))


def render_index(config: dict, entries: list[Entry]) -> str:
    descriptions = config["entry_descriptions"]
    parts = [
        "<main>",
        f"  <h1>{html.escape(config['site_title'])}</h1>",
        f'  <p class="site-description">{html.escape(config["site_description"])}</p>',
        '  <ul class="entry-list">',
    ]
    for entry in entries:
        date_value = datetime.strptime(entry.key, "%Y%m%d")
        link_label = f"{date_value.month}/{date_value.day}"
        description = descriptions.get(entry.key, f"ここに{format_date_key(entry.key)}の日記の説明を入力してください。")
        parts.append(
            f'    <li><a href="{entry.output_name}">{link_label}</a>: '
            f'{html.escape(str(description))}</li>'
        )
    parts.extend([
        "  </ul>",
        '  <hr>',
        f'  <a href="{html.escape(config["parent_index_url"], quote=True)}">'
        f'{html.escape(config["parent_index_label"])}</a>',
        "</main>",
    ])
    return page_shell(config["site_title"], "\n".join(parts))


CSS = """\
:root { color-scheme: light; font-family: "Noto Sans JP", "Yu Gothic", sans-serif; }
body { margin: 0; color: #222; background: #fff; line-height: 1.8; }
main { width: min(100% - 2rem, 70rem); margin: 2rem auto 4rem; }
h1 { line-height: 1.35; }
.site-description { white-space: pre-line; }
.entry-list { padding-left: 1.5rem; }
.entry-list li { margin-block: .45rem; }
.diary-entry p { margin: 1rem 0; }
.photo-row { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0; align-items: flex-start; }
.photo-row img { width: auto; height: auto; max-width: 100%; border-radius: 2px; }
.entry-navigation { display: flex; flex-wrap: wrap; gap: .8rem 1.4rem; margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid #aaa; }
a { color: #0645ad; }
@media (max-width: 40rem) {
  main { width: min(100% - 1.25rem, 70rem); margin-top: 1rem; }
  .photo-row img { max-width: 100%; }
}
"""


def warn_placeholders(config: dict, entries: list[Entry]) -> None:
    warnings = []
    for key in ("site_title", "site_description"):
        if PLACEHOLDER_MARKER in config[key]:
            warnings.append(key)
    for entry in entries:
        value = str(config["entry_descriptions"].get(entry.key, ""))
        if not value or PLACEHOLDER_MARKER in value:
            warnings.append(f"entry_descriptions.{entry.key}")
    if warnings:
        print("Warning: placeholders remain in: " + ", ".join(warnings), file=sys.stderr)


def safe_output_path(root: Path, configured: str) -> Path:
    output = (root / configured).resolve()
    if output == root or root not in output.parents:
        raise ValueError("output_directory must be a subdirectory of the source folder")
    return output


def build(root: Path, config_name: str, clean: bool) -> None:
    root = root.resolve()
    diary_paths = discover_diaries(root)
    if not diary_paths:
        raise ValueError("No yyyymmdd.txt diary files found")
    config_path = root / config_name
    initialize_or_extend_config(config_path, diary_paths)
    config = load_config(config_path)
    validate_config(config)
    entries = [parse_entry(path) for path in diary_paths]
    photos = image_files(root / config["photo_directory"])
    photo_map = referenced_photos(entries, photos)
    output = safe_output_path(root, config["output_directory"])
    if clean and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    thumbnail_output = output / config["thumbnail_directory"]
    for photo in sorted(set(photo_map.values())):
        resize_image(photo, thumbnail_output / photo.name, config["thumbnail_max_size"])
    for index, entry in enumerate(entries):
        previous = entries[index - 1] if index else None
        following = entries[index + 1] if index + 1 < len(entries) else None
        content = render_entry(entry, previous, following, photo_map, config["thumbnail_directory"])
        (output / entry.output_name).write_text(content, encoding="utf-8")
    (output / "index.html").write_text(render_index(config, entries), encoding="utf-8")
    (output / "style.css").write_text(CSS, encoding="utf-8")
    warn_placeholders(config, entries)
    print(f"Built {len(entries)} diary pages and {len(photo_map)} thumbnails in {output}")


def main() -> int:
    args = parse_args()
    try:
        build(args.source, args.config, args.clean)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
