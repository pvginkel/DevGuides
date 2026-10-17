# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "pathspec>=0.12"]
# ///
"""Builds and checks every guide in a source repo of https://dev-guides.home/.

    uv run tools/check_guide.py <repo-dir>...
    uv run https://raw.githubusercontent.com/pvginkel/DevGuides/main/tools/check_guide.py <repo-dir>

A guide is a top-level folder of the repo that holds a mkdocs.yml. Each one is built from its
folder with its own toolchain, `uv run --locked zensical build --strict`, into site/. The checks
below cover what that strict build does not, by the guide contract's item:

- guide (1): no mkdocs.yml at the repo root, where it would be built by nothing.
- site-url (2): site_url is https://dev-guides.home/<path>/, one path segment, and no two guides of
  the repo share it. build_hub.py checks the same across repos.
- title (3): site_name and site_description are set; the hub's index and root llms.txt show them.
- toolchain (4): pyproject.toml and uv.lock beside mkdocs.yml, the lock holding zensical.
- build (4): the strict build exits 0.
- llms (5): the llmstxt plugin has `full_output: llms-full.txt` and one section of one pattern,
  "*.md"; after the build every page has its Markdown copy, its entry in llms.txt and its copy
  inside llms-full.txt.
- nav (6): every page is in the nav, and every nav entry names a page. Zensical ignores
  `validation.nav`: it builds an orphan page, and renders a nav entry naming no page as a dead link.
- links (6): no root-absolute link in a page, in Markdown, a reference definition or raw HTML.
  Zensical checks only those ending in .md, and a site moved to another path breaks the rest.
- pages (6): no directory symlink inside docs_dir. Zensical does not walk one and says nothing, so
  its pages are silently not built.
- home (7): the nav links home, to https://dev-guides.home/.
- own-checks (8): the guide's check_site.py, when it has one, passes after the build, run with the
  guide's toolchain as `uv run --locked python check_site.py`.
- config (warning only): a top-level mkdocs.yml key Zensical does not read, and the validation
  settings it ignores. Zensical drops both without a word.

The pages are listed from the source, as Zensical lists them: the Markdown files under docs_dir,
without the default excludes (`.*`, `/templates/`) and exclude_docs' gitignore patterns, file
symlinks followed, directory symlinks not. No MkDocs import: Zensical depends on neither MkDocs nor
Material.

Exit status: 0 when every guide passes, 1 on any error, 2 on a usage error. Warnings do not fail.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import pathspec
import yaml

HUB = "https://dev-guides.home/"
SITE_URL = re.compile(r"https://dev-guides\.home/([A-Za-z0-9][A-Za-z0-9._-]*)/")
CONFIG = "mkdocs.yml"
OWN_CHECKS = "check_site.py"

# The contract item each rule enforces, for the messages.
ITEMS = {
    "guide": 1,
    "site-url": 2,
    "title": 3,
    "toolchain": 4,
    "build": 4,
    "llms": 5,
    "nav": 6,
    "links": 6,
    "pages": 6,
    "home": 7,
    "own-checks": 8,
}

# The top-level mkdocs.yml keys Zensical reads (zensical/config.py, 0.0.71). It ignores any other
# without a warning, so a misspelt key or a MkDocs-only one would pass unnoticed.
KNOWN_KEYS = {
    "INHERIT",
    "copyright",
    "dev_addr",
    "docs_dir",
    "draft_docs",
    "edit_uri",
    "edit_uri_template",
    "exclude_docs",
    "extra",
    "extra_css",
    "extra_javascript",
    "extra_templates",
    "markdown_extensions",
    "nav",
    "not_in_nav",
    "plugins",
    "remote_branch",
    "remote_name",
    "repo_name",
    "repo_url",
    "site_author",
    "site_description",
    "site_dir",
    "site_name",
    "site_url",
    "strict",
    "theme",
    "use_directory_urls",
    "validation",
    "watch",
}

# MkDocs' and Zensical's default exclusions from docs_dir, before exclude_docs.
DEFAULT_EXCLUDES = [".*", "/templates/"]

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+).+?\1")
# A root-absolute target: one leading slash, not two (`//host/` is another host, not this site).
ROOT_ABSOLUTE = [
    re.compile(r"\]\(\s*<?(/(?!/)[^)\s>]*)"),  # [text](/x) and ![alt](/x)
    re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(/(?!/)[^\s>]*)"),  # [ref]: /x
    re.compile(r"""\b(?:href|src)\s*=\s*["'](/(?!/)[^"']*)["']""", re.IGNORECASE),  # raw HTML
]
# An llms.txt entry, `- [Title](<url>)` as Zensical writes it, or `- [Title](url)`.
LLMS_ENTRY = re.compile(r"^- \[.*\]\((?:<([^>]+)>|([^)\s]+))\)", re.MULTILINE)


class _Tolerant(yaml.SafeLoader):
    """Reads any mkdocs.yml: a tagged value (!ENV, !relative, !!python/name:…) becomes a
    placeholder. The keys this script reads are never tagged."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"__tag__": node.tag, "value": value}


_Tolerant.add_multi_constructor("!", _tagged)
_Tolerant.add_multi_constructor("tag:yaml.org,2002:python/", _tagged)


@dataclass
class Problem:
    severity: str  # "error" or "warning"
    where: str  # <repo>/<guide>/<file>[:<line>]
    rule: str
    message: str

    def __str__(self) -> str:
        item = ITEMS.get(self.rule)
        rule = f"{self.rule} (item {item})" if item else self.rule
        return f"{self.severity}: {self.where}: {rule}: {self.message}"


@dataclass
class Guide:
    repo: Path
    folder: Path
    config: dict = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    built: bool = False

    @property
    def label(self) -> str:
        return f"{self.repo.name}/{self.folder.name}"

    @property
    def site(self) -> Path:
        return self.folder / "site"

    @property
    def site_url(self) -> str | None:
        value = self.config.get("site_url")
        return value if isinstance(value, str) else None

    @property
    def path(self) -> str | None:
        """The guide's one path segment under the hub, when site_url has the contract's shape."""
        match = SITE_URL.fullmatch(self.site_url or "")
        return match[1] if match else None

    @property
    def name(self) -> str:
        return " ".join(str(self.config.get("site_name") or "").split())

    @property
    def description(self) -> str:
        return " ".join(str(self.config.get("site_description") or "").split())

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "error"]

    def error(self, file: str, rule: str, message: str) -> None:
        self.problems.append(Problem("error", self._where(file), rule, message))

    def warn(self, file: str, rule: str, message: str) -> None:
        self.problems.append(Problem("warning", self._where(file), rule, message))

    def _where(self, file: str) -> str:
        return f"{self.label}/{file}" if file else self.label


def find_guides(repo: Path) -> list[Guide]:
    return [
        Guide(repo, folder)
        for folder in sorted(repo.iterdir())
        if folder.is_dir() and not folder.name.startswith(".") and (folder / CONFIG).is_file()
    ]


def load_config(guide: Guide) -> bool:
    """Reads mkdocs.yml into guide.config; False when there is nothing to build from."""
    try:
        config = yaml.load((guide.folder / CONFIG).read_text(encoding="utf8"), Loader=_Tolerant)
    except yaml.YAMLError as error:
        guide.error(CONFIG, "build", f"not readable as YAML: {error}")
        return False
    if not isinstance(config, dict):
        guide.error(CONFIG, "build", "not a mapping of settings")
        return False
    guide.config = config
    docs_dir = config.get("docs_dir", "docs")
    if not isinstance(docs_dir, str) or not (guide.folder / docs_dir).is_dir():
        guide.error(CONFIG, "build", f"docs_dir {docs_dir!r} is not a directory of the guide")
        return False
    return True


def _plugins(config: dict) -> dict[str, object]:
    plugins = config.get("plugins") or []
    if isinstance(plugins, dict):
        return dict(plugins)
    found: dict[str, object] = {}
    for item in plugins if isinstance(plugins, list) else []:
        if isinstance(item, str):
            found[item] = {}
        elif isinstance(item, dict) and len(item) == 1:
            found.update(item)
    return found


def check_config(guide: Guide) -> None:
    config = guide.config
    for key in sorted(set(config) - KNOWN_KEYS, key=str):
        why = "Zensical does not run hooks" if key == "hooks" else "Zensical does not read it"
        guide.warn(CONFIG, "config", f"top-level key {key!r} is ignored: {why}")
    validation = config.get("validation")
    if isinstance(validation, dict):
        ignored = [f"validation.{key}" for key in validation if key == "nav"]
        links = validation.get("links")
        if isinstance(links, dict):
            ignored += [
                f"validation.links.{key}"
                for key in links
                if key in ("absolute_links", "unrecognized_links")
            ]
        for key in ignored:
            guide.warn(
                CONFIG, "config", f"{key} is ignored by Zensical; this checker covers it, delete it"
            )

    url = guide.site_url
    if guide.path is None:
        guide.error(
            CONFIG, "site-url", f"site_url is {url!r}, not {HUB}<path>/ with one path segment"
        )
    for key in ("site_name", "site_description"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            guide.error(CONFIG, "title", f"{key} is not set; the hub's index and llms.txt show it")

    for name in ("pyproject.toml", "uv.lock"):
        if not (guide.folder / name).is_file():
            guide.error(name, "toolchain", "missing: the guide carries its own locked toolchain")
    lock = guide.folder / "uv.lock"
    if lock.is_file() and not re.search(r'^name = "zensical"$', lock.read_text("utf8"), re.M):
        guide.error("uv.lock", "toolchain", "does not lock zensical")

    llmstxt = _plugins(config).get("llmstxt")
    if not isinstance(llmstxt, dict):
        guide.error(CONFIG, "llms", "the llmstxt plugin is not configured")
    else:
        if llmstxt.get("full_output") != "llms-full.txt":
            guide.error(CONFIG, "llms", "llmstxt's full_output is not llms-full.txt")
        sections = llmstxt.get("sections")
        if not (
            isinstance(sections, dict)
            and len(sections) == 1
            and list(sections.values())[0] == ["*.md"]
        ):
            guide.error(
                CONFIG,
                "llms",
                'llmstxt needs one section holding the one pattern "*.md", so that no page is '
                "left out of llms.txt",
            )

    nav = config.get("nav")
    if not isinstance(nav, list) or not nav:
        guide.error(CONFIG, "nav", "no nav: every page is listed in it, with the home link")
    elif HUB not in nav_entries(nav):
        guide.error(CONFIG, "home", f"the nav does not link home, to {HUB}")


def nav_entries(nav: object) -> list[str]:
    """Every target in the nav, pages and URLs, at any depth."""
    if isinstance(nav, str):
        return [nav.strip()]
    if isinstance(nav, list):
        return [entry for item in nav for entry in nav_entries(item)]
    if isinstance(nav, dict):
        return [entry for value in nav.values() for entry in nav_entries(value)]
    return []


def list_pages(guide: Guide) -> None:
    docs = guide.folder / guide.config.get("docs_dir", "docs")
    docs_rel = docs.relative_to(guide.folder).as_posix()
    exclude = guide.config.get("exclude_docs") or ""
    spec = pathspec.GitIgnoreSpec.from_lines(
        DEFAULT_EXCLUDES + (exclude.splitlines() if isinstance(exclude, str) else [])
    )
    pages = []
    # Not following links, as Zensical's walk does not: a directory symlink is listed, never
    # entered.
    for root, dirs, files in os.walk(docs):
        rel_root = Path(root).relative_to(docs)
        for name in dirs:
            if (Path(root) / name).is_symlink():
                rel = (rel_root / name).as_posix()
                target = os.readlink(Path(root) / name)
                guide.error(
                    f"{docs_rel}/{rel}",
                    "pages",
                    f"a directory symlink (to {target}): Zensical does not walk it, so the "
                    "pages under it are not built. Use a real directory",
                )
        for name in files:
            rel = (rel_root / name).as_posix()
            if not name.endswith(".md") or spec.match_file(rel):
                continue
            if not (Path(root) / name).exists():
                guide.error(f"{docs_rel}/{rel}", "pages", "a symlink to nothing")
                continue
            pages.append(rel)
    guide.pages = sorted(pages)


def check_nav(guide: Guide) -> None:
    nav = guide.config.get("nav")
    if not isinstance(nav, list):
        return
    targets = [e for e in nav_entries(nav) if not urlsplit(e).scheme]
    docs_rel = guide.config.get("docs_dir", "docs")
    for page in guide.pages:
        if page not in targets:
            guide.error(f"{docs_rel}/{page}", "nav", "not in the nav; give it a nav row")
    for target in targets:
        if target not in guide.pages:
            guide.error(
                CONFIG, "nav", f"the nav entry {target!r} names no page; it would be a dead link"
            )


def _prose_lines(text: str) -> list[str]:
    """The page's lines with fenced code blocks blanked and inline code spans erased."""
    lines, fence = [], None
    for line in text.split("\n"):
        if fence:
            stripped = line.strip()
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = None
            lines.append("")
            continue
        opened = FENCE_OPEN.match(line)
        if opened:
            fence = opened[1]
            lines.append("")
            continue
        lines.append(INLINE_CODE.sub(lambda m: " " * len(m[0]), line))
    return lines


def check_links(guide: Guide) -> None:
    docs_rel = guide.config.get("docs_dir", "docs")
    docs = guide.folder / docs_rel
    for page in guide.pages:
        text = (docs / page).read_text(encoding="utf8")
        for number, line in enumerate(_prose_lines(text), start=1):
            for pattern in ROOT_ABSOLUTE:
                for match in pattern.finditer(line):
                    guide.error(
                        f"{docs_rel}/{page}:{number}",
                        "links",
                        f"root-absolute link {match[1]!r}: it breaks when the guide's path "
                        "changes; link relative to this page",
                    )


def _run(guide: Guide, command: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, NO_COLOR="1")
    # Under `uv run` of this script, VIRTUAL_ENV names this script's environment, not the guide's.
    env.pop("VIRTUAL_ENV", None)
    env.setdefault("UV_LINK_MODE", "copy")
    return subprocess.run(
        command, cwd=guide.folder, env=env, capture_output=True, text=True, check=False
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    text = ANSI.sub("", (result.stdout or "") + (result.stderr or "")).strip()
    return "\n".join(f"    | {line}" for line in text.splitlines())


def build(guide: Guide) -> None:
    # From the guide's folder: pymdownx.snippets resolves a relative base_path from the cwd.
    result = _run(guide, ["uv", "run", "--locked", "zensical", "build", "--strict"])
    if result.returncode != 0:
        guide.error(
            CONFIG,
            "build",
            f"`uv run --locked zensical build --strict` exited {result.returncode}:\n"
            + _output(result),
        )
        return
    guide.built = True


def _copy_uri(page: str) -> str:
    path = PurePosixPath(page)
    return page if path.name == "index.md" else (path.with_suffix("") / "index.md").as_posix()


def check_llms(guide: Guide) -> None:
    llms_file, full_file = guide.site / "llms.txt", guide.site / "llms-full.txt"
    for file in (llms_file, full_file):
        if not file.is_file():
            guide.error(f"site/{file.name}", "llms", "the build wrote no such file")
    if not (llms_file.is_file() and full_file.is_file()):
        return
    entries = {a or b for a, b in LLMS_ENTRY.findall(llms_file.read_text(encoding="utf8"))}
    full = full_file.read_text(encoding="utf8")
    docs_rel = guide.config.get("docs_dir", "docs")
    for page in guide.pages:
        uri = _copy_uri(page)
        copy = guide.site / uri
        where = f"{docs_rel}/{page}"
        if not copy.is_file():
            guide.error(where, "llms", f"no Markdown copy at site/{uri}")
            continue
        if f"{guide.site_url}{uri}" not in entries:
            guide.error(where, "llms", f"not listed in llms.txt as {guide.site_url}{uri}")
        if copy.read_text(encoding="utf8").strip() not in full:
            guide.error(where, "llms", "its Markdown copy is not inside llms-full.txt")


def run_own_checks(guide: Guide) -> None:
    if not (guide.folder / OWN_CHECKS).is_file():
        return
    result = _run(guide, ["uv", "run", "--locked", "python", OWN_CHECKS])
    if result.returncode != 0:
        guide.error(OWN_CHECKS, "own-checks", f"exited {result.returncode}:\n" + _output(result))


def check_guide(guide: Guide) -> None:
    if not load_config(guide):
        return
    check_config(guide)
    list_pages(guide)
    check_nav(guide)
    check_links(guide)
    build(guide)
    if guide.built:
        check_llms(guide)
        run_own_checks(guide)


def check_repo(repo: Path, log=print) -> tuple[list[Guide], list[Problem]]:
    """Builds and checks every guide of the repo. Returns the guides, and the repo's problems that
    belong to no one guide; each guide holds its own."""
    repo = repo.resolve()
    problems: list[Problem] = []
    if (repo / CONFIG).is_file():
        problems.append(
            Problem(
                "error",
                f"{repo.name}/{CONFIG}",
                "guide",
                "a mkdocs.yml at the repo root is no guide and is built by nothing; a guide is a "
                "top-level folder that holds one",
            )
        )
    guides = find_guides(repo)
    for guide in guides:
        log(f"check_guide: building and checking {guide.label}", flush=True)
        check_guide(guide)
    by_path: dict[str, list[Guide]] = {}
    for guide in guides:
        if guide.path:
            by_path.setdefault(guide.path, []).append(guide)
    for path, sharing in by_path.items():
        if len(sharing) > 1:
            names = ", ".join(g.label for g in sharing)
            problems.append(
                Problem("error", repo.name, "site-url", f"{names} all publish at /{path}/")
            )
    return guides, problems


def main(argv: list[str]) -> int:
    if not argv or any(arg.startswith("-") for arg in argv):
        print("usage: check_guide.py <repo-dir>...", file=sys.stderr)
        return 2
    failed = False
    for arg in argv:
        repo = Path(arg)
        if not repo.is_dir():
            print(f"check_guide: {arg}: no such directory", file=sys.stderr)
            return 2
        guides, problems = check_repo(repo)
        problems = problems + [p for guide in guides for p in guide.problems]
        for problem in problems:
            print(problem, flush=True)
        errors = [p for p in problems if p.severity == "error"]
        warnings = len(problems) - len(errors)
        failed = failed or bool(errors)
        names = (
            ", ".join(f"{g.folder.name} ({f'/{g.path}/' if g.path else 'no path'})" for g in guides)
            or "no guides"
        )
        verdict = f"{len(errors)} error(s)" if errors else "pass"
        print(f"check_guide: {repo.resolve().name}: {names}: {verdict}, {warnings} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
