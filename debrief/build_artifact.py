"""Build the debrief site as a Claude artifact in ARTIFACT_OUT.

The host wraps the page in its own html/head/body, so page.html holds only the title, the
Google Fonts links, the inlined CSS, the body markup and the inlined scripts. The exported
data/ is copied next to it. Run debrief.export_site first.

    uv run python -m debrief.build_artifact
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

from debrief.paths import ARTIFACT_OUT as OUT
from debrief.paths import SITE_OUT
from debrief.paths import SITE_SRC as SITE

MAX_FILES = 255
MAX_FILE_BYTES = 16_000_000  # decimal, to stay on the safe side of the host limit
MAX_TOTAL_BYTES = 64_000_000


def one(pattern: str, text: str, what: str) -> str:
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if not m:
        sys.exit(f"build_artifact: no {what} in site/index.html")
    return m.group(1)


def inline_script(path: Path) -> str:
    # "</script" inside JS would end the tag early; "<\/script" means the same thing in a JS string.
    js = re.sub(r"</(script)", r"<\\/\1", path.read_text(), flags=re.IGNORECASE)
    return f"<script>\n{js}\n</script>"


def build_page() -> str:
    html = (SITE / "index.html").read_text()
    head = one(r"<head>(.*?)</head>", html, "<head>")
    body = one(r"<body[^>]*>(.*?)</body>", html, "<body>")
    title = one(r"(<title>.*?</title>)", head, "<title>")
    fonts = [tag for tag in re.findall(r"<link\b[^>]*>", head, re.IGNORECASE) if "fonts.g" in tag]
    scripts = re.findall(r'<script src="([^"]+)"></script>', body)
    if scripts != ["method.js", "app.js"]:
        sys.exit(f"build_artifact: expected method.js then app.js, found {scripts}")
    markup = re.sub(r"\s*<script\b[^>]*></script>", "", body).strip()
    css = (SITE / "style.css").read_text()
    parts = [title, *fonts, f"<style>\n{css}\n</style>", markup, *(inline_script(SITE / s) for s in scripts)]
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    if not (SITE_OUT / "data" / "index.json").exists():
        sys.exit(f"build_artifact: {SITE_OUT / 'data' / 'index.json'} missing; run debrief.export_site first")
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    (OUT / "page.html").write_text(build_page())
    shutil.copytree(SITE_OUT / "data", OUT / "data")

    files = sorted(p for p in OUT.rglob("*") if p.is_file())
    sizes = {p: p.stat().st_size for p in files}
    total = sum(sizes.values())
    largest = max(sizes, key=sizes.get)
    print(f"{len(files)} files, {total:,} bytes ({total / 1e6:.1f} MB)")
    print(f"largest: {largest.relative_to(OUT)} at {sizes[largest]:,} bytes ({sizes[largest] / 1e6:.1f} MB)")

    errors = []
    if len(files) > MAX_FILES:
        errors.append(f"{len(files)} files exceeds the {MAX_FILES}-file limit")
    errors += [f"{p.relative_to(OUT)} is {n:,} bytes, over the {MAX_FILE_BYTES:,}-byte file limit"
               for p, n in sizes.items() if n > MAX_FILE_BYTES]
    if total > MAX_TOTAL_BYTES:
        errors.append(f"{total:,} bytes exceeds the {MAX_TOTAL_BYTES:,}-byte total limit")
    if errors:
        sys.exit("build_artifact: over limits:\n  " + "\n  ".join(errors))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
