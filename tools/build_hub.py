# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "pathspec>=0.12"]
# ///
"""Builds https://dev-guides.home/ into out/: every guide of every source repo, the index page and
the root llms.txt. This is the one definition of the site's layout: the Dockerfile and this repo's
`kc project test` both run it.

    uv run tools/build_hub.py [<root>]

<root> holds the repos sources.txt lists, side by side under their GitHub names: the Jenkins
workspace after Checkout, /work in a pod. It defaults to the directory this repo is in.

1. tools/check_guide.py builds and checks every guide of every source repo;
2. no two guides claim the same path, and none claims one of the hub's own files;
3. out/<path>/ is each guide's site/;
4. out/index.html is landing/index.html with one card per guide: its title, its description, a
   link to the guide and one to its llms.txt. landing/'s other files are copied beside it;
5. out/llms.txt lists every guide's llms.txt with its description;
6. every link on the index page and in the root llms.txt resolves in out/.

The dependencies above are check_guide.py's, which this imports.
"""

from __future__ import annotations

import html
import importlib.util
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SOURCES = REPO / "sources.txt"
LANDING = REPO / "landing"
OUT = REPO / "out"
MARKER = "<!-- guides -->"
TITLE = "Dev guides"
SUMMARY = "Rules and guidelines for agents, one guide per subsite."


def _import_checker():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("check_guide", HERE / "check_guide.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses look their module up in sys.modules.
    sys.modules["check_guide"] = module
    spec.loader.exec_module(module)
    return module


check_guide = _import_checker()
HUB = check_guide.HUB


def _fail(errors: list[str], message: str) -> None:
    print(f"error: {message}", flush=True)
    errors.append(message)


def read_sources() -> list[str]:
    lines = (line.strip() for line in SOURCES.read_text(encoding="utf8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def check_sources(root: Path, errors: list[str]) -> list:
    """Steps 1 and 2: every guide of every source repo, built and checked."""
    guides = []
    for name in read_sources():
        repo = root / name
        if not repo.is_dir():
            _fail(
                errors, f"{repo}: missing; sources.txt names it, so it is cloned beside DevGuides"
            )
            continue
        repo_guides, problems = check_guide.check_repo(repo)
        problems += [p for guide in repo_guides for p in guide.problems]
        for problem in problems:
            print(problem, flush=True)
            if problem.severity == "error":
                errors.append(str(problem))
        guides += repo_guides

    reserved = {"index.html", "llms.txt"} | {p.name for p in LANDING.iterdir()}
    by_path: dict[str, list] = {}
    for guide in guides:
        if guide.path:
            by_path.setdefault(guide.path, []).append(guide)
    for path, claiming in sorted(by_path.items()):
        names = ", ".join(g.label for g in claiming)
        if len(claiming) > 1:
            _fail(errors, f"{names}: all publish at /{path}/; a path is one guide's")
        if path in reserved:
            _fail(errors, f"{names}: publishes at /{path}/, which is the hub's own {path}")
    return sorted(guides, key=lambda g: g.name.casefold())


def _card(guide, indent: str) -> str:
    path = html.escape(guide.path, quote=True)
    lines = [
        '<section class="guide">',
        f'  <h2><a href="{path}/">{html.escape(guide.name)}</a></h2>',
        f"  <p>{html.escape(guide.description)}</p>",
        f'  <p><a href="{path}/llms.txt">llms.txt</a>: the same guide for agents</p>',
        "</section>",
    ]
    return "\n".join(indent + line for line in lines)


def write_index(guides: list) -> None:
    """Step 4."""
    template = (LANDING / "index.html").read_text(encoding="utf8")
    if template.count(MARKER) != 1:
        raise SystemExit(f"build_hub: landing/index.html must hold {MARKER} exactly once")
    line = next(line for line in template.splitlines() if MARKER in line)
    indent = line[: len(line) - len(line.lstrip())]
    cards = "\n".join(_card(guide, indent) for guide in guides).lstrip()
    (OUT / "index.html").write_text(template.replace(MARKER, cards), encoding="utf8")
    for file in LANDING.iterdir():
        if file.name != "index.html":
            shutil.copy(file, OUT / file.name)


def write_llms(guides: list) -> None:
    """Step 5."""
    lines = [f"# {TITLE}", "", f"> {SUMMARY}", "", "## Guides", ""]
    lines += [f"- [{g.name}]({g.site_url}llms.txt): {g.description}" for g in guides]
    (OUT / "llms.txt").write_text("\n".join(lines) + "\n", encoding="utf8")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.links += [value for name, value in attrs if name in ("href", "src") and value]


def _served(link: str) -> Path | None:
    """The file in out/ that the served hub answers a link with, or None. A link on the index page
    is relative to /, a link in llms.txt is absolute on the hub."""
    path = urlsplit(link[len(HUB) - 1 :] if link.startswith(HUB) else link).path
    target = OUT / path.lstrip("/")
    if path.endswith("/") or target.is_dir():
        target = target / "index.html"
    return target if target.is_file() else None


def check_links(errors: list[str]) -> None:
    """Step 6."""
    parser = _Links()
    parser.feed((OUT / "index.html").read_text(encoding="utf8"))
    for link in parser.links:
        parts = urlsplit(link)
        if (parts.scheme or parts.netloc) and not link.startswith(HUB):
            continue  # another site's link is not the hub's to resolve
        if not parts.path and parts.fragment:
            continue
        if _served(link) is None:
            _fail(errors, f"out/index.html: {link} resolves to nothing in out/")
    llms = (OUT / "llms.txt").read_text(encoding="utf8")
    for match in check_guide.LLMS_ENTRY.finditer(llms):
        link = match[1] or match[2]
        if not link.startswith(HUB):
            _fail(errors, f"out/llms.txt: {link} is not on {HUB}")
        elif _served(link) is None:
            _fail(errors, f"out/llms.txt: {link} resolves to nothing in out/")


def main(argv: list[str]) -> int:
    if len(argv) > 1 or any(arg.startswith("-") for arg in argv):
        print("usage: build_hub.py [<root>]", file=sys.stderr)
        return 2
    root = Path(argv[0]).resolve() if argv else REPO.parent
    errors: list[str] = []
    guides = check_sources(root, errors)
    if errors:
        print(f"build_hub: {len(errors)} error(s); nothing assembled into out/", file=sys.stderr)
        return 1

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    for guide in guides:
        shutil.copytree(guide.site, OUT / guide.path)
    write_index(guides)
    write_llms(guides)
    check_links(errors)
    if errors:
        print(f"build_hub: {len(errors)} error(s) in out/", file=sys.stderr)
        return 1
    names = ", ".join(f"{g.name} (/{g.path}/)" for g in guides)
    print(f"build_hub: out/ holds {len(guides)} guide(s) from {root}: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
