"""Check tracked documentation links, heading fragments and SVG/JSON syntax."""

import json
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'!?\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+"[^"]*")?\)')


def prose(text):
    return re.sub(r"^(`{3,}|~{3,}).*?^\1\s*$", "", text, flags=re.M | re.S)


def headings(path):
    text = prose(path.read_text())
    found = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', text))
    counts = {}
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        title = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"<[^>]+>", "", title).lower()
        slug = "".join(c for c in title if c in "-_ " or unicodedata.category(c)[0] in "LN")
        slug = slug.replace(" ", "-")
        number = counts.get(slug, 0)
        found.add(slug + ("-" + str(number) if number else ""))
        counts[slug] = number + 1
    return found


def link_errors(path, tracked):
    errors = []
    text = re.sub(r"`[^`\n]+`", "", prose(path.read_text()))
    for match in LINK.finditer(text):
        target = match.group(1)
        uri = urlsplit(target)
        if uri.scheme or uri.netloc:
            continue
        destination = (path.parent / unquote(uri.path)).resolve() if uri.path else path
        try:
            name = destination.relative_to(ROOT).as_posix()
        except ValueError:
            errors.append(f"{path.relative_to(ROOT)}: target escapes repository: {target}")
            continue
        exists = (
            name in tracked
            if destination.is_file()
            else any(p.startswith(name.rstrip("/") + "/") for p in tracked)
        )
        if not destination.exists() or not exists:
            errors.append(f"{path.relative_to(ROOT)}: missing tracked target: {target}")
        elif (
            destination.suffix == ".md"
            and uri.fragment
            and unquote(uri.fragment) not in headings(destination)
        ):
            errors.append(f"{path.relative_to(ROOT)}: missing heading: {target}")
    return errors


def main():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    tracked = {name for name in names if name and (ROOT / name).is_file()}
    documents = [ROOT / name for name in sorted(tracked) if name.startswith("docs/")]
    errors = []
    for path in [ROOT / "README.md", *documents]:
        try:
            if path.suffix == ".md":
                errors.extend(link_errors(path, tracked))
            elif path.suffix == ".svg":
                ET.parse(path)
            elif path.suffix == ".json":
                json.loads(path.read_text())
        except (ValueError, ET.ParseError) as error:
            errors.append(f"{path.relative_to(ROOT)}: {error}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Documentation valid: {len(documents)} tracked files; links, fragments, SVG and JSON")


if __name__ == "__main__":
    main()
