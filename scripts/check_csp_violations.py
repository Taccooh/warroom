"""
CI check: flag content that the app's Content-Security-Policy would block
in the browser (or that has quietly drifted out of the allow-list).

The policy (app/security.py) is read directly out of its source via
_CSP_DIRECTIVES, so this check tracks the policy automatically as it
changes — including which checks even apply:

  - if script-src has no 'unsafe-inline': inline <script>...</script>
    blocks with no src= (and that aren't a non-executable data island like
    application/json), and inline onclick="..."/onchange="...  (and
    similar) attributes, are both flagged — script-src governs both.
  - if style-src has no 'unsafe-inline': inline style="..." attributes
    are flagged.
  - always: <script src="...">, dynamic .src = "...", and
    fetch()/EventSource()/WebSocket() calls pointing at a host that isn't
    allow-listed in the current policy.

Right now style-src *does* carry 'unsafe-inline' (a deliberate, narrow
exception for a handful of dynamic per-value styles — see the comment in
app/security.py) so inline style="..." is not flagged today. If that
exception is ever removed, this check starts enforcing it with no changes
needed here.

A flagged line can be deliberately allowed with a trailing
``csp-ignore`` comment (any comment syntax) if it's a false positive.

Usage (as a pre-commit hook, CI step, or standalone):
    python scripts/check_csp_violations.py <file> [<file> ...]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECURITY_HEADERS_PATH = PROJECT_ROOT / "app" / "security.py"

# Paths where we don't own the content, so it's not ours to fix.
EXCLUDED_PATH_PARTS = (
    "/static/vendor/",   # vendored Leaflet (BSD-2-Clause) — third-party
    "/.git/",
)

NON_EXECUTABLE_SCRIPT_TYPES = ("application/json", "application/ld+json")

# Excludes data-custom-style="..." (a hyphenated data-* attribute name,
# not a real style attribute).
STYLE_ATTR_RE = re.compile(r'(?<![-\w(])style\s*=\s*["\']')
EVENT_ATTR_RE = re.compile(
    r'\bon(?:click|change|submit|load|error|input|mouseover|mouseout|'
    r'mouseenter|mouseleave|focus|blur|keydown|keyup|keypress|dblclick|'
    r'contextmenu|drag|dragstart|dragend|dragover|drop|touchstart|'
    r'touchend|touchmove)\s*=\s*["\']'
)
SCRIPT_OPEN_TAG_RE = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*["\']')
TYPE_ATTR_RE = re.compile(r'\btype\s*=\s*["\']([^"\']+)["\']')

HTML_EXTERNAL_SRC_RE = re.compile(
    r'<(script|link)\b[^>]*?\b(?:src|href)\s*=\s*"(https?://[^"]+)"',
    re.IGNORECASE,
)
JS_DYNAMIC_SRC_RE = re.compile(
    r'\.src\s*=\s*[`"\'](https?://[^`"\']+)[`"\']'
)
JS_CONNECT_RE = re.compile(
    r'(?:fetch|new\s+EventSource|new\s+WebSocket)\s*\(\s*'
    r'[`"\'](https?://[^`"\']+)[`"\']'
)


def _load_csp_directives() -> dict[str, str]:
    source = SECURITY_HEADERS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SECURITY_HEADERS_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_CSP_DIRECTIVES"
            for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(
        f"Could not find _CSP_DIRECTIVES in {SECURITY_HEADERS_PATH}"
    )


def _domains_for(directives: dict[str, str], key: str) -> set[str]:
    value = directives.get(key, "")
    domains = set()
    for token in value.split():
        if token.startswith("http://") or token.startswith("https://"):
            domains.add(urlparse(token).netloc)
    return domains


def _allows_unsafe_inline(directives: dict[str, str], key: str) -> bool:
    return "'unsafe-inline'" in directives.get(key, "")


def is_excluded(path: str) -> bool:
    normalized = "/" + path.replace("\\", "/")
    return any(part in normalized for part in EXCLUDED_PATH_PARTS)


def _line_ignored(line: str) -> bool:
    return "csp-ignore" in line


def _is_comment_line(path: str, line: str) -> bool:
    """Best-effort check so comments *about* these patterns don't
    themselves get flagged. Deliberately simple — doesn't handle every
    multi-line block-comment shape, but covers this codebase's style."""
    stripped = line.strip()
    if path.endswith(".py"):
        return stripped.startswith("#")
    if path.endswith(".js"):
        return stripped.startswith(("//", "*", "/*"))
    if path.endswith(".html"):
        return stripped.startswith(("<!--", "*")) or stripped.startswith("{#")
    return False


def check_inline_style(path: str, lines: list[str]) -> list[str]:
    errors = []
    for i, line in enumerate(lines, start=1):
        if _line_ignored(line) or _is_comment_line(path, line):
            continue
        if STYLE_ATTR_RE.search(line):
            errors.append(
                f"{path}:{i}: inline style=\"...\" attribute — blocked by "
                f"style-src (no 'unsafe-inline'). Use a CSS class instead."
            )
    return errors


def check_inline_event_handlers(path: str, lines: list[str]) -> list[str]:
    errors = []
    for i, line in enumerate(lines, start=1):
        if _line_ignored(line) or _is_comment_line(path, line):
            continue
        match = EVENT_ATTR_RE.search(line)
        if match:
            errors.append(
                f"{path}:{i}: inline {match.group(0).split('=')[0]}=\"...\" "
                f"handler — blocked by script-src (no 'unsafe-inline'). "
                f"Attach the listener from an external .js file instead "
                f"(see app/static/warroom.js for the data-confirm/"
                f"addEventListener pattern already used here)."
            )
    return errors


def check_inline_scripts(path: str, lines: list[str]) -> list[str]:
    errors = []
    for i, line in enumerate(lines, start=1):
        if _line_ignored(line) or _is_comment_line(path, line):
            continue
        for match in SCRIPT_OPEN_TAG_RE.finditer(line):
            attrs = match.group(1)
            if SRC_ATTR_RE.search(attrs):
                continue
            type_match = TYPE_ATTR_RE.search(attrs)
            if (
                type_match
                and type_match.group(1).lower() in NON_EXECUTABLE_SCRIPT_TYPES
            ):
                continue
            errors.append(
                f"{path}:{i}: inline <script> block with no src= — blocked "
                f"by script-src (no 'unsafe-inline'). Move the code to a "
                f"static .js file and load it with <script src=...>, "
                f"handing over per-request data via a "
                f"<script type=\"application/json\"> island instead "
                f"(see app/templates/warroom.html + app/static/warroom.js)."
            )
    return errors


def check_external_hosts(
    path: str,
    lines: list[str],
    script_domains: set[str],
    style_domains: set[str],
    connect_domains: set[str],
) -> list[str]:
    errors = []
    is_js = path.endswith(".js")
    for i, line in enumerate(lines, start=1):
        if _line_ignored(line) or _is_comment_line(path, line):
            continue
        for tag, url in HTML_EXTERNAL_SRC_RE.findall(line):
            domain = urlparse(url).netloc
            allowed = (
                script_domains if tag.lower() == "script" else style_domains
            )
            if domain not in allowed:
                errors.append(
                    f"{path}:{i}: {tag} references https://{domain}/..., "
                    f"which is not in the CSP allow-list "
                    f"(app/security.py). Add it there, or switch to an "
                    f"already-allowed host."
                )
        if is_js:
            for url in JS_DYNAMIC_SRC_RE.findall(line):
                domain = urlparse(url).netloc
                if domain not in script_domains:
                    errors.append(
                        f"{path}:{i}: dynamic script src points at "
                        f"https://{domain}/..., which is not in "
                        f"script-src's allow-list."
                    )
            for url in JS_CONNECT_RE.findall(line):
                domain = urlparse(url).netloc
                if domain not in connect_domains:
                    errors.append(
                        f"{path}:{i}: request to https://{domain}/..., "
                        f"which is not in connect-src's allow-list "
                        f"(app/security.py)."
                    )
    return errors


def check_file(path: str, directives: dict[str, str]) -> list[str]:
    if is_excluded(path):
        return []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()

    style_ok = _allows_unsafe_inline(directives, "style-src")
    script_ok = _allows_unsafe_inline(directives, "script-src")

    errors: list[str] = []
    if not style_ok and path.endswith((".html", ".py", ".js")):
        errors += check_inline_style(path, lines)
    if not script_ok and path.endswith((".html", ".py")):
        errors += check_inline_event_handlers(path, lines)
    if not script_ok and path.endswith(".html"):
        errors += check_inline_scripts(path, lines)
    if path.endswith((".html", ".js")):
        errors += check_external_hosts(
            path,
            lines,
            _domains_for(directives, "script-src"),
            _domains_for(directives, "style-src"),
            _domains_for(directives, "connect-src"),
        )
    return errors


def main(argv: list[str]) -> int:
    if not argv:
        return 0

    directives = _load_csp_directives()
    all_errors: list[str] = []
    for path in argv:
        all_errors.extend(check_file(path, directives))

    if all_errors:
        print("CSP violation check failed:\n")
        for err in all_errors:
            print(f"  {err}")
        print(
            "\nIf a line is a false positive, append a 'csp-ignore' "
            "comment to it to allow it through."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
