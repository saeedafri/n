#!/usr/bin/env python3
"""Upgrade Mermaid diagrams in docs/technical-documentation/*.md for team review."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC_DIR = REPO / "docs" / "technical-documentation"

CLASS_DEFS = """
    classDef start fill:#e8f4fd,stroke:#1e88e5,color:#0d47a1
    classDef db fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef error fill:#ffebee,stroke:#e53935,color:#b71c1c
    classDef ext fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef process fill:#eceff1,stroke:#546e7a,color:#263238"""

MERMAID_FENCE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
FLOWCHART_START = re.compile(r"^flowchart\s+(TB|TD|LR|RL|BT)\b", re.MULTILINE)
SEQUENCE_START = re.compile(r"^sequenceDiagram\b", re.MULTILINE)

# Caption hints when heading/section is generic
SECTION_CAPTIONS: dict[str, str] = {
    "### Client-side navigation flow": "Header navigation uses parent-frame JavaScript to click hidden Streamlit page links while showing a branded transition overlay.",
    "### Tab selection flow": "Market Data tab changes persist in query params and local storage, then rerun the page to load the selected tab content.",
    "### Data flow": "Company Profile loads overview data from the repository layer and renders HTML table blocks for the active ticker.",
    "### Auth flow": "End-to-end OIDC login: IdP authorization, cookie handoff via parent-frame JavaScript, and per-page identity checks.",
    "### Logout flow": "Logout routing branches on OIDC mode — stash context and RP-initiated IdP logout, or legacy session clear.",
    "### Two-layer model": "Access checks consult system role first (admin bypass), then page-level ACL rows in MySQL.",
    "### Extraction flow": "LLM metric extraction checks caches first, then builds context windows and calls GPT-4o-mini before persisting results.",
    "### Pipeline": "Screening narrows the company universe through progressive criterion filters until the working set matches all active rules.",
    "## Flowcharts": "Visual reference for cross-module authentication and request lifecycle flows.",
    "### Full request lifecycle": "Every HTTP request passes through boot, auth hydrate, optional logout routing, conditional warmups, then the active Streamlit page.",
    "### Login → Home cookie handoff (cross-module)": "After OIDC login, the browser sets `auth_session` via JS and `main.py` verifies the cookie before rendering Home.",
    "### OIDC login (happy path)": "Standard SSO journey from button click through token exchange to cookie handoff and server verification.",
    "### User interaction — SSO button": "The SSO button injects parent-frame JavaScript to navigate to the IdP authorize URL after a brief overlay.",
    "### Data flow — token to session": "Token response is normalized to user identity, written to session state and audit DB, then slim cookie is set in the browser.",
    "### Error handling — access denied": "Non-@coresight.com emails trigger IdP logout and block reprocessing of the denied authorization code.",
    "### Step 1 — Recover `id_token` (priority order)": "Logout bridge walks a fixed priority list of session, cookie, and temp-file sources to recover `id_token_hint`.",
    "### Step 3 — Atomic JS clear + redirect": "Parent-frame JavaScript clears cookies atomically, then navigates to IdP logout or login fallback.",
    "### Entry from navigation": "User logout starts in navigation, stashes OIDC context in `main.py`, then routes to the invisible logout bridge page.",
    "### Cookie clearing (JS)": "Both auth cookies are expired on host-only and `.coresight.com` paths before redirect.",
    "### Cross-host post-logout return": "Cross-host logout stores return origin in a temp file; login page meta-refreshes the browser back to the original host.",
}


def _has_class_defs(body: str) -> bool:
    return "classDef " in body


def _is_flowchart(body: str) -> bool:
    return bool(FLOWCHART_START.search(body.strip()))


def _is_sequence(body: str) -> bool:
    return bool(SEQUENCE_START.search(body.strip()))


def _normalize_flowchart_direction(body: str) -> str:
    """Prefer TD for vertical flows, LR for pipelines with --> chains and few nodes."""

    def replacer(match: re.Match[str]) -> str:
        direction = match.group(1)
        if direction == "TB":
            return "flowchart TD"
        return match.group(0)

    return FLOWCHART_START.sub(replacer, body, count=1)


def _apply_node_classes(body: str) -> str:
    """Assign semantic classes to common node id patterns."""
    lines = body.split("\n")
    class_lines: list[str] = []
    ids: dict[str, str] = {}

    for line in lines:
        # Database nodes: [(...)] or explicit DB/MYSQL labels
        m = re.match(r"\s*(\w+)\[.*\]", line) or re.match(r"\s*(\w+)\(\[.*\]\)", line)
        if m:
            nid = m.group(1)
            low = line.lower()
            if "[(" in line or "mysql" in low or "coreiq_" in low or "db]" in low or "database" in low:
                ids[nid] = "db"
            elif any(x in low for x in ("error", "deny", "fail", "invalid", "stop")):
                ids[nid] = "error"
            elif any(x in low for x in ("streamlit", "browser", "ui", "overlay", "header", "page")):
                ids[nid] = "ui"
            elif any(x in low for x in ("idp", "smtp", "openai", "blob", "azure", "oidc")):
                ids[nid] = "ext"
            elif any(x in low for x in ("start", "boot", "login", "user click")):
                ids[nid] = "start"

        m2 = re.match(r"\s*(\w+)\{", line)
        if m2:
            ids[m2.group(1)] = "process"

    if ids:
        grouped: dict[str, list[str]] = {}
        for nid, cls in ids.items():
            grouped.setdefault(cls, []).append(nid)
        for cls, nids in grouped.items():
            class_lines.append(f"    class {','.join(nids)} {cls}")

    if class_lines and "class " not in body:
        body = body.rstrip() + "\n" + "\n".join(class_lines)
    return body


def _inject_class_defs(body: str) -> str:
    if _has_class_defs(body):
        return body
    body = body.rstrip() + CLASS_DEFS
    return _apply_node_classes(body)


def _needs_caption(prev_line: str) -> bool:
    prev = prev_line.strip()
    if not prev:
        return True
    if prev.startswith("|") or prev.startswith("```"):
        return True
    if prev.endswith(":") and not prev.endswith(".):"):
        return False
    # Already a caption-like sentence
    if len(prev) > 40 and (prev.endswith(".") or prev.startswith("The ") or prev.startswith("This ")):
        return False
    if prev.startswith("#"):
        return True
    return False


def _caption_for_context(lines: list[str], idx: int) -> str | None:
    """Find nearest heading above mermaid block."""
    for j in range(idx - 1, max(idx - 15, -1), -1):
        line = lines[j].strip()
        if line in SECTION_CAPTIONS:
            return SECTION_CAPTIONS[line]
        if line.startswith("## ") or line.startswith("### "):
            title = line.lstrip("#").strip()
            if title in SECTION_CAPTIONS:
                return SECTION_CAPTIONS[title]
            # Generic caption from heading
            if "flow" in title.lower() or "architecture" in title.lower() or "pipeline" in title.lower():
                return f"This diagram shows the {title.lower()} for this module."
    return None


def upgrade_mermaid_block(body: str) -> str:
    body = body.strip("\n")
    if _is_flowchart(body):
        body = _normalize_flowchart_direction(body)
        body = _inject_class_defs(body)
    return body + "\n"


def process_file(path: Path) -> tuple[int, int, bool]:
    text = path.read_text(encoding="utf-8")
    before = len(MERMAID_FENCE.findall(text))
    changed = False
    lines = text.split("\n")
    new_parts: list[str] = []
    last_end = 0

    for match in MERMAID_FENCE.finditer(text):
        start, end = match.span()
        new_parts.append(text[last_end:start])

        # Check for caption
        block_start_line = text[:start].count("\n")
        prev_line = lines[block_start_line - 1] if block_start_line > 0 else ""

        if _needs_caption(prev_line):
            caption = _caption_for_context(lines, block_start_line)
            if caption and caption not in text[max(0, start - 200):start]:
                new_parts.append(caption + "\n\n")
                changed = True

        old_body = match.group(1)
        new_body = upgrade_mermaid_block(old_body)
        if new_body != old_body + ("\n" if not old_body.endswith("\n") else ""):
            changed = True
        new_parts.append("```mermaid\n" + new_body + "```")
        last_end = end

    new_parts.append(text[last_end:])
    new_text = "".join(new_parts)
    after = len(MERMAID_FENCE.findall(new_text))

    if changed:
        path.write_text(new_text, encoding="utf-8")

    return before, after, changed


def main() -> int:
    total_before = 0
    total_after = 0
    edited: list[str] = []

    for path in sorted(DOC_DIR.rglob("*.md")):
        before, after, changed = process_file(path)
        total_before += before
        total_after += after
        if changed:
            edited.append(path.name)

    print(f"Diagrams before: {total_before}")
    print(f"Diagrams after: {total_after}")
    print(f"Files edited: {len(edited)}")
    for name in edited:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
