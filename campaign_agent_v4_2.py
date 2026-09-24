#!/usr/bin/env python3
"""
campaign_agent_v4_2.py
Conservative Obsidian campaign-note processor: Qwen extracts; Python validates, asks approval, and writes.

Features
--------
- Processes one Markdown note at a time.
- Scans the vault for existing NPC/location/faction/quest/item/session notes and validates configured folders.
- Uses Ollama to extract candidate entities plus evidence-backed campaign state.
- Python resolves candidates against filenames, YAML names/titles, headings, and aliases.
- Adds [[wikilinks]] to existing entities.
- Updates YAML metadata on the target note.
- Can create missing entity notes from templates.
- Can add a reverse `related:` link to matched/created entity notes.
- Dry-run, approval, and auto modes.
- Backs up every file before modifying it.

Requirements
------------
Python 3.10+
PyYAML:
    python3 -m pip install pyyaml

Ollama running locally:
    open -a Ollama

Example
-------
    python3 campaign_agent_v4.py "Sessions/Session 38.md" --dry-run
    python3 campaign_agent_v4.py "Sessions/Session 38.md"
    python3 campaign_agent_v4.py "Sessions/Session 38.md" --auto
"""

from __future__ import annotations

import argparse
import time
import difflib
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML")
    print("Install it with: python3 -m pip install pyyaml")
    sys.exit(1)


ENTITY_TYPES = ("npc", "location", "faction", "quest", "item")
TYPE_TO_METADATA_KEY = {
    "npc": "npcs",
    "location": "locations",
    "faction": "factions",
    "quest": "quests",
    "item": "items",
}
DEFAULT_TYPE_FOLDERS = {
    "npc": "NPCs",
    "location": "Locations",
    "faction": "Factions",
    "quest": "Quests",
    "item": "Items",
    "session": "Sessions",
}
DEFAULT_TEMPLATE_FILES = {
    "npc": "NPC Template.md",
    "location": "Location Template.md",
    "faction": "Faction Template.md",
    "quest": "Quest Template.md",
    "item": "Item Template.md",
    "session": "Session Template.md",
}


@dataclass
class VaultNote:
    path: Path
    rel_path: str
    title: str
    note_type: str | None
    aliases: list[str]
    frontmatter: dict[str, Any]
    body: str


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a YAML mapping.")
    return data


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, flags=re.S)
    if not match:
        return {}, text

    raw = match.group(1)
    try:
        fm = yaml.safe_load(raw) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}

    return fm, text[match.end():]


def dump_markdown(frontmatter: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    return f"---\n{yaml_text}\n---\n\n{body.lstrip()}"


def normalize_name(value: str) -> str:
    value = re.sub(r"\[\[|\]\]", "", str(value))
    value = value.split("|", 1)[0]
    value = value.strip().lower().replace("’", "'")
    value = re.sub(r"[^a-z0-9']+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def name_variants(value: str) -> set[str]:
    """Conservative deterministic variants used only for vault matching."""
    base = normalize_name(value)
    if not base:
        return set()
    variants = {base}

    # Leading articles are common in prose but often absent from filenames.
    for article in ("the ", "a ", "an "):
        if base.startswith(article) and len(base) > len(article) + 2:
            variants.add(base[len(article):].strip())

    # Apostrophe punctuation should not prevent an otherwise exact match.
    variants |= {v.replace("'", "") for v in list(variants)}

    return {v for v in variants if v}


def creation_policy(entity: dict[str, Any]) -> tuple[str, str]:
    """
    Return (status, reason):
      eligible - specific named candidate; may auto-create
      review   - potentially useful, but requires human approval
      blocked  - too generic to create

    `significance` is supplied by the extractor when available:
      incidental | meaningful | recurring
    """
    mention = str(entity.get("mention") or entity.get("name") or "").strip()
    etype = str(entity.get("type") or "").lower().strip()
    significance = str(entity.get("significance") or "meaningful").lower().strip()
    key = normalize_name(mention)
    if not key:
        return "blocked", "empty name"

    generic_exact = {
        "the bar", "bar", "the city", "city", "the town", "town",
        "the village", "village", "the sewers", "sewers",
        "the academy", "academy", "the temple", "temple",
        "the inn", "inn", "the tavern", "tavern",
        "the cleric", "cleric", "the priest", "priest",
        "the head cleric", "head cleric", "the head priest", "head priest",
        "the guard", "guard", "the guards", "guards",
    }
    if key in generic_exact:
        return "blocked", "generic description/title"

    if len(key.split()) == 1 and len(key) < 5:
        return "blocked", "too short/generic"

    # Unnamed but meaningful/recurring NPCs are worth tracking as review candidates.
    if etype == "npc":
        unnamed_patterns = (
            "youth", "woman", "man", "sailor", "cleric", "priest",
            "matron", "guard", "healer", "proprietor", "representative", "rep"
        )
        looks_descriptive = (
            key.startswith("the ")
            or any(key.endswith(" " + role) for role in unnamed_patterns)
        )
        if looks_descriptive:
            if significance in {"meaningful", "recurring"}:
                return "review", f"unnamed {significance} NPC; potentially worth tracking"
            return "blocked", "unnamed incidental NPC"

    if etype == "faction":
        return "review", "new faction classification requires review"

    if any(key.endswith(term) for term in (" helm", " tattoo")):
        return "review", "entity type/name is potentially ambiguous"

    return "eligible", "specific named candidate"


def is_low_specificity_entity(entity: dict[str, Any]) -> bool:
    return creation_policy(entity)[0] == "blocked"


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    return [str(value)]


def wikilink(title: str) -> str:
    return f"[[{title}]]"


def note_display_title(path: Path, fm: dict[str, Any], body: str) -> str:
    for key in ("name", "title"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            return re.sub(r"^\[\[|\]\]$", "", val.strip())

    heading = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    if heading:
        return heading.group(1).strip()

    return path.stem


def infer_type(path: Path, fm: dict[str, Any], type_folders: dict[str, str]) -> str | None:
    t = fm.get("type")
    if isinstance(t, str) and t.strip():
        return t.strip().lower()

    parts_lower = [p.lower() for p in path.parts]
    for entity_type, folder in type_folders.items():
        if folder.lower() in parts_lower:
            return entity_type
    return None


def read_note(path: Path, vault_root: Path, type_folders: dict[str, str]) -> VaultNote:
    text = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    title = note_display_title(path, fm, body)
    aliases = as_list(fm.get("aliases"))
    return VaultNote(
        path=path,
        rel_path=str(path.relative_to(vault_root)),
        title=title,
        note_type=infer_type(path.relative_to(vault_root), fm, type_folders),
        aliases=aliases,
        frontmatter=fm,
        body=body,
    )


def should_skip(path: Path, vault_root: Path, exclude_folders: list[str]) -> bool:
    rel = path.relative_to(vault_root)
    excluded = {x.strip("/").lower() for x in exclude_folders}
    return any(part.lower() in excluded for part in rel.parts)


def scan_vault(
    vault_root: Path,
    target_path: Path,
    type_folders: dict[str, str],
    exclude_folders: list[str],
) -> list[VaultNote]:
    notes: list[VaultNote] = []
    for path in vault_root.rglob("*.md"):
        if path.resolve() == target_path.resolve():
            continue
        if should_skip(path, vault_root, exclude_folders):
            continue
        try:
            notes.append(read_note(path, vault_root, type_folders))
        except Exception as exc:
            print(f"Warning: could not read {path}: {exc}")
    return notes


def build_alias_index(notes: list[VaultNote]) -> dict[str, list[VaultNote]]:
    index: dict[str, list[VaultNote]] = {}
    for note in notes:
        names = [
            note.title,
            note.path.stem,
            note.frontmatter.get("name"),
            note.frontmatter.get("title"),
            *note.aliases,
        ]
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            for key in name_variants(name):
                bucket = index.setdefault(key, [])
                if note not in bucket:
                    bucket.append(note)
    return index


def ollama_chat(base_url: str, model: str, prompt: str, temperature: float, timeout: int) -> str:
    endpoint = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract structured campaign information from D&D notes. "
                    "Never invent facts. Return valid JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": temperature},
    }

    # Newer Ollama builds support think:false for Qwen3. If an older build
    # rejects it, retry without it.
    payload["think"] = False

    def do_request(data: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        response = do_request(payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and "think" in payload:
            payload.pop("think", None)
            response = do_request(payload)
        else:
            raise

    content = response.get("message", {}).get("content", "").strip()
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    return content


def extract_entities(
    target: VaultNote,
    existing_notes: list[VaultNote],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Qwen extracts evidence-backed campaign state. Python still owns vault matching
    and all write decisions.
    """
    prompt = f"""
Read the D&D session note and extract durable campaign knowledge useful in Obsidian.

Do NOT invent facts. Distinguish established facts from rumors, suspicions, and open
questions. Preserve uncertainty exactly. Output JSON only and keep it compact.

ENTITY TYPES
- npc: named people/creatures, plus unnamed individuals only when they perform a
  meaningful action or appear to be recurring
- location: specifically named places
- faction: specifically named organizations/groups
- quest: explicit missions/investigations/objectives
- item: specifically named/notable objects

ENTITY SIGNIFICANCE
- incidental: background mention with no durable campaign importance
- meaningful: participates in a meaningful scene, clue, quest, or decision
- recurring: explicitly connected to an earlier appearance/event or strongly
  indicated as someone the party may encounter again

CAMPAIGN-STATE CATEGORIES
- quests: accepted/offered missions and objectives, including current status
- clues: concrete observations/evidence that may matter later
- developments: meaningful changes or newly established campaign facts
- rumors: claims heard from others that are not independently established
- open_questions: unresolved mysteries/suspicions; never turn a question into fact
- todos: explicit party/player follow-up actions

For every campaign-state entry include:
- title: concise label
- detail: one concise evidence-backed statement
- related: entity names from the note when useful
- status: use a short value such as active, unresolved, observed, reported, suspected,
  completed, or planned

Rules:
- Use only information supported by this note.
- Do not add generic D&D lore.
- Do not decide whether a note already exists in the vault.
- Do not generate free-form relationships.
- Do not convert suspicions into facts.
- Do not treat every place/name as important merely because it is capitalized.
- Deduplicate repeated information.
- An unnamed recurring/meaningful NPC may be included with a stable descriptive name,
  e.g. "Blond-Haired Youth", but incidental unnamed people should be omitted.

Return JSON exactly in this shape:
{{
  "entities": [
    {{
      "mention": "text as written",
      "name": "concise stable name",
      "type": "npc|location|faction|quest|item",
      "significance": "incidental|meaningful|recurring",
      "reason": "brief evidence for why this is worth tracking"
    }}
  ],
  "quests": [
    {{"title":"...", "detail":"...", "related":[], "status":"..."}}
  ],
  "clues": [
    {{"title":"...", "detail":"...", "related":[], "status":"..."}}
  ],
  "developments": [
    {{"title":"...", "detail":"...", "related":[], "status":"..."}}
  ],
  "rumors": [
    {{"title":"...", "detail":"...", "related":[], "status":"..."}}
  ],
  "open_questions": [
    {{"title":"...", "detail":"...", "related":[], "status":"..."}}
  ],
  "todos": [
    {{"title":"...", "detail":"...", "related":[], "status":"planned"}}
  ]
}}

TARGET NOTE: {target.rel_path}
=========================
{target.body}
"""

    raw = ollama_chat(
        config.get("ollama_url", "http://localhost:11434"),
        config.get("model", "qwen3:1.7b"),
        prompt,
        float(config.get("temperature", 0.1)),
        int(config.get("ollama_timeout_seconds", 120)),
    )

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as first_error:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        candidate = match.group(0) if match else None
        try:
            if candidate is None:
                raise first_error
            result = json.loads(candidate)
        except json.JSONDecodeError as parse_error:
            debug_dir = target.path.parent.parent / "_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_path = debug_dir / f"ollama_failed_response_{stamp}.txt"
            debug_path.write_text(raw, encoding="utf-8")
            lines = raw.splitlines()
            line_no = max(1, parse_error.lineno)
            lo = max(0, line_no - 4)
            hi = min(len(lines), line_no + 3)
            nearby = "\n".join(f"{i + 1:>6}: {lines[i]}" for i in range(lo, hi))
            raise ValueError(
                "Ollama returned malformed JSON.\n"
                f"Parse failure: line {parse_error.lineno}, column {parse_error.colno}, "
                f"char {parse_error.pos}.\n"
                f"Raw response length: {len(raw):,} characters.\n"
                f"Raw response saved to: {debug_path}\n"
                "Nearby Ollama output:\n"
                "----------------------------------------\n"
                f"{nearby}\n"
                "----------------------------------------"
            ) from parse_error

    if not isinstance(result, dict):
        result = {}
    for key in ("entities", "quests", "clues", "developments", "rumors", "open_questions", "todos"):
        if not isinstance(result.get(key), list):
            result[key] = []
    result["relationships"] = []
    return result


def resolve_entity(
    entity: dict[str, Any],
    alias_index: dict[str, list[VaultNote]],
    fuzzy_threshold: float,
) -> tuple[VaultNote | None, str]:
    etype = str(entity.get("type", "")).lower()

    # Exact deterministic matching across mention/name variants.
    for value in (entity.get("name"), entity.get("mention")):
        if not value:
            continue
        for key in name_variants(str(value)):
            hits = alias_index.get(key, [])
            typed = [n for n in hits if n.note_type == etype]
            if len(typed) == 1:
                return typed[0], "exact-type"
            if len(hits) == 1:
                return hits[0], "exact"

    # Conservative fuzzy matching. Prefer same-type notes and require a unique hit.
    candidate_keys = set()
    for value in (entity.get("name"), entity.get("mention")):
        if value:
            candidate_keys |= name_variants(str(value))

    best: tuple[VaultNote, float] | None = None
    second_best = 0.0
    seen_notes: set[Path] = set()
    for candidate in candidate_keys:
        for key, hits in alias_index.items():
            ratio = difflib.SequenceMatcher(None, candidate, key).ratio()
            if ratio < fuzzy_threshold:
                continue
            for note in hits:
                if note.path in seen_notes or (note.note_type and note.note_type != etype):
                    continue
                seen_notes.add(note.path)
                if best is None or ratio > best[1]:
                    if best is not None:
                        second_best = max(second_best, best[1])
                    best = (note, ratio)
                else:
                    second_best = max(second_best, ratio)

    # Require separation from the runner-up to avoid dangerous fuzzy matches.
    if best and best[1] >= fuzzy_threshold and (best[1] - second_best >= 0.04 or second_best == 0):
        return best[0], f"fuzzy:{best[1]:.2f}"

    return None, "new"


def in_protected_range(text: str, start: int, end: int) -> bool:
    # Existing wikilinks
    for m in re.finditer(r"\[\[.*?\]\]", text):
        if start < m.end() and end > m.start():
            return True

    # Inline code and fenced code
    for m in re.finditer(r"`[^`\n]*`|```.*?```", text, flags=re.S):
        if start < m.end() and end > m.start():
            return True

    return False


def replace_mentions_with_links(
    body: str,
    replacements: list[tuple[str, str]],
) -> tuple[str, list[tuple[str, str]]]:
    # Longest mentions first reduces partial collisions.
    replacements = sorted(
        {(m.strip(), title.strip()) for m, title in replacements if m and title},
        key=lambda x: len(x[0]),
        reverse=True,
    )

    changed: list[tuple[str, str]] = []
    result = body

    for mention, canonical in replacements:
        # Skip obviously dangerous ultra-short aliases.
        if len(normalize_name(mention)) < 3:
            continue

        pattern = re.compile(
            rf"(?<![\w\]]){re.escape(mention)}(?![\w\[])",
            flags=re.I,
        )

        pieces = []
        last = 0
        did_change = False
        for match in pattern.finditer(result):
            if in_protected_range(result, match.start(), match.end()):
                continue
            pieces.append(result[last:match.start()])
            visible = match.group(0)
            if normalize_name(visible) == normalize_name(canonical):
                link = f"[[{canonical}]]"
            else:
                link = f"[[{canonical}|{visible}]]"
            pieces.append(link)
            last = match.end()
            did_change = True

        if did_change:
            pieces.append(result[last:])
            result = "".join(pieces)
            changed.append((mention, canonical))

    return result, changed


def unique_links(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        key = normalize_name(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def update_target_frontmatter(
    fm: dict[str, Any],
    target: VaultNote,
    resolved: list[tuple[dict[str, Any], VaultNote | None]],
) -> dict[str, Any]:
    fm = dict(fm)
    if target.note_type and not fm.get("type"):
        fm["type"] = target.note_type

    grouped: dict[str, list[str]] = {t: [] for t in ENTITY_TYPES}
    for entity, note in resolved:
        etype = str(entity.get("type", "")).lower()
        if etype not in grouped:
            continue
        if note is None:
            continue
        canonical = note.title
        if canonical:
            grouped[etype].append(wikilink(canonical))

    for etype, links in grouped.items():
        if not links:
            continue
        key = TYPE_TO_METADATA_KEY[etype]
        existing = as_list(fm.get(key))
        fm[key] = unique_links(existing + links)

    return fm


def backup_file(path: Path, vault_root: Path, backup_dir: Path) -> Path:
    rel = path.relative_to(vault_root)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = backup_dir / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def render_template(
    template_path: Path | None,
    title: str,
    entity_type: str,
    source_title: str,
) -> str:
    if template_path and template_path.exists():
        text = template_path.read_text(encoding="utf-8")
        text = text.replace("{{title}}", title)
        fm, body = split_frontmatter(text)
    else:
        fm = {
            "type": entity_type,
            "name": title,
            "aliases": [],
            "related": [],
            "tags": [entity_type],
        }
        body = (
            f"# {title}\n\n"
            "## Summary\n\n"
            "Created by campaign_agent_v4.py. Review and expand this note.\n\n"
            "## Party Knowledge\n\n"
            "## Session History\n\n"
            "## Secrets / DM Knowledge\n"
        )

    fm["type"] = entity_type
    if not fm.get("name"):
        fm["name"] = title

    related = as_list(fm.get("related"))
    source_link = wikilink(source_title)
    if source_link not in related:
        related.append(source_link)
    fm["related"] = related

    return dump_markdown(fm, body)


def safe_filename(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*]', "-", title).strip().rstrip(".")
    return title or "Untitled"


def create_entity_note(
    entity: dict[str, Any],
    vault_root: Path,
    source_title: str,
    type_folders: dict[str, str],
    template_folder: str,
    template_files: dict[str, str],
) -> Path:
    etype = str(entity["type"]).lower()
    title = str(entity.get("name") or entity.get("mention")).strip()
    folder = vault_root / type_folders[etype]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_filename(title)}.md"

    template_path = vault_root / template_folder / template_files[etype]
    text = render_template(
        template_path if template_path.exists() else None,
        title,
        etype,
        source_title,
    )
    path.write_text(text, encoding="utf-8")
    return path


def add_reverse_related_link(
    note_path: Path,
    source_title: str,
) -> bool:
    text = note_path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    related = as_list(fm.get("related"))
    link = wikilink(source_title)
    if link in related:
        return False
    related.append(link)
    fm["related"] = related
    note_path.write_text(dump_markdown(fm, body), encoding="utf-8")
    return True


def resolve_target(vault_root: Path, arg: str) -> Path:
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = vault_root / p
    p = p.resolve()
    if not p.exists():
        raise FileNotFoundError(f"Target note not found: {p}")
    if p.suffix.lower() != ".md":
        raise ValueError("Target must be a Markdown (.md) file.")
    try:
        p.relative_to(vault_root.resolve())
    except ValueError:
        raise ValueError("Target note must be inside the configured vault.")
    return p


def clean_campaign_state(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    cleaned: dict[str, list[dict[str, Any]]] = {}
    for category in ("quests", "clues", "developments", "rumors", "open_questions", "todos"):
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in result.get(category, []):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            detail = str(raw.get("detail") or "").strip()
            if not title or not detail:
                continue
            key = (normalize_name(title), normalize_name(detail))
            if key in seen:
                continue
            seen.add(key)
            related = [str(x).strip() for x in as_list(raw.get("related")) if str(x).strip()]
            rows.append({
                "title": title,
                "detail": detail,
                "related": related,
                "status": str(raw.get("status") or "").strip(),
            })
        cleaned[category] = rows
    return cleaned


def print_campaign_state(state: dict[str, list[dict[str, Any]]]) -> None:
    labels = {
        "quests": "QUESTS / OBJECTIVES",
        "clues": "CLUES / EVIDENCE",
        "developments": "CAMPAIGN DEVELOPMENTS",
        "rumors": "RUMORS / REPORTED CLAIMS",
        "open_questions": "OPEN QUESTIONS / MYSTERIES",
        "todos": "PARTY TODOs",
    }
    print("\n" + "=" * 72)
    print("CAMPAIGN STATE EXTRACTED FROM THIS SESSION")
    print("=" * 72)
    any_rows = False
    for category, label in labels.items():
        rows = state.get(category, [])
        if not rows:
            continue
        any_rows = True
        print(f"\n{label}")
        for row in rows:
            status = f" [{row['status']}]" if row.get("status") else ""
            print(f"  • {row['title']}{status}")
            print(f"    {row['detail']}")
            if row.get("related"):
                print(f"    Related: {', '.join(row['related'])}")
    if not any_rows:
        print("  (none)")
    print("\nNOTE: Campaign-state sections are report-only in V4.2.")
    print("They are NOT written into lore/plot notes yet; this lets us validate the")
    print("extraction before deciding the final vault folders/templates.")
    print("=" * 72)


def validate_type_folders(
    vault_root: Path,
    type_folders: dict[str, str],
    create_missing: bool,
) -> list[str]:
    """
    Refuse to silently invent configured entity folders when note creation is enabled.
    Sessions is allowed to be the current target folder; all configured paths must exist.
    """
    problems: list[str] = []
    if not create_missing:
        return problems
    for etype, folder in type_folders.items():
        path = vault_root / folder
        if not path.exists() or not path.is_dir():
            problems.append(f"{etype}: {folder}/")
    return problems


def print_plan(
    target: VaultNote,
    resolved: list[tuple[dict[str, Any], VaultNote | None, str]],
    create_missing: bool,
    conflict_keys: set[str] | None = None,
) -> None:
    print("\n" + "=" * 72)
    print("CAMPAIGN AGENT V4 PLAN")
    print("=" * 72)
    print(f"Target: {target.rel_path}\n")

    conflict_keys = conflict_keys or set()
    matched, eligible, review, blocked, conflicts = [], [], [], [], []

    for entity, note, method in resolved:
        key = normalize_name(entity.get("mention") or entity.get("name") or "")
        if key in conflict_keys:
            conflicts.append((entity, note, method))
        elif note:
            matched.append((entity, note, method))
        else:
            status, reason = creation_policy(entity)
            row = (entity, note, method, reason)
            if status == "eligible":
                eligible.append(row)
            elif status == "review":
                review.append(row)
            else:
                blocked.append(row)

    print("MATCHED EXISTING NOTES")
    if not matched:
        print("  (none)")
    for entity, note, method in matched:
        print(f"  ✓ {entity.get('mention')!r} -> [[{note.title}]] "
              f"[{entity.get('type')}, {method}]")

    print("\nPROPOSED NEW NOTES")
    if not eligible:
        print("  (none)")
    for entity, _, _, reason in eligible:
        action = "eligible for creation" if create_missing else "creation disabled"
        significance = entity.get("significance", "meaningful")
        print(f"  ? {entity.get('mention')!r} -> {entity.get('name')} "
              f"[{entity.get('type')}, {significance}, {action}]")
        if entity.get("reason"):
            print(f"      Why: {entity.get('reason')}")

    print("\nREVIEW-ONLY CANDIDATES")
    if not review:
        print("  (none)")
    for entity, _, _, reason in review:
        significance = entity.get("significance", "meaningful")
        print(f"  ? {entity.get('mention')!r} [{entity.get('type')}, {significance}] - {reason}")
        if entity.get("reason"):
            print(f"      Why: {entity.get('reason')}")

    print("\nBLOCKED / LOW-SPECIFICITY")
    if not blocked:
        print("  (none)")
    for entity, _, _, reason in blocked:
        print(f"  ! {entity.get('mention')!r} [{entity.get('type')}] - {reason}")

    print("\nTYPE CONFLICTS")
    if not conflicts:
        print("  (none)")
    for entity, note, _ in conflicts:
        matched_text = f" -> [[{note.title}]]" if note else ""
        print(f"  ⚠ {entity.get('mention')!r}: {entity.get('type')}{matched_text} "
              f"- requires review")

    print("\nWrite policy:")
    print("  - Existing matches are safe link candidates.")
    print("  - Normal mode asks individually before creating each proposed/review note.")
    print("  - --auto creates only eligible new notes; review/blocked/conflict candidates are skipped.")
    print("  - YAML receives only matched or actually-created notes.")
    print("  - No model-generated semantic relationships are written.")
    print("  - Existing files are backed up before modification.")
    print("=" * 72)



def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def select_new_entities(
    resolved_full: list[tuple[dict[str, Any], VaultNote | None, str]],
    conflict_keys: set[str],
    create_missing: bool,
    auto_mode: bool,
    dry_run: bool,
) -> set[tuple[str, str]]:
    """
    Return normalized (mention, type) keys approved for creation.
    Dry-run never prompts. In --auto, only 'eligible' candidates are approved.
    In normal mode, eligible and review candidates are presented individually.
    """
    approved: set[tuple[str, str]] = set()
    if not create_missing:
        return approved

    for entity, note, _ in resolved_full:
        if note is not None:
            continue
        mention = str(entity.get("mention") or entity.get("name") or "").strip()
        etype = str(entity.get("type") or "").lower()
        norm = normalize_name(mention)
        if norm in conflict_keys:
            continue

        status, reason = creation_policy(entity)
        key = (norm, etype)

        if dry_run:
            continue
        if auto_mode:
            if status == "eligible":
                approved.add(key)
            continue
        if status == "blocked":
            continue

        label = "NEW ENTITY" if status == "eligible" else "REVIEW CANDIDATE"
        print(f"\n{label}: {mention}")
        print(f"Type: {etype}")
        if status == "review":
            print(f"Reason for review: {reason}")
        if ask_yes_no(f"Create [[{entity.get('name') or mention}]]?"):
            approved.add(key)

    return approved

def _main_impl() -> int:
    parser = argparse.ArgumentParser(description="Process one Obsidian campaign note using local Ollama.")
    parser.add_argument("note", help="Target Markdown note, relative to vault root or absolute.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML (default: config.yaml).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show proposed changes; write nothing.")
    mode.add_argument("--auto", action="store_true", help="Apply changes without confirmation.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_yaml_file(config_path)

    vault_raw = str(config.get("vault_path", "")).strip()
    if not vault_raw or vault_raw.startswith("/CHANGE/"):
        print("Set vault_path in config.yaml first.")
        return 2

    vault_root = Path(vault_raw).expanduser().resolve()
    if not vault_root.exists():
        print(f"Vault path does not exist: {vault_root}")
        return 2

    type_folders = dict(DEFAULT_TYPE_FOLDERS)
    type_folders.update(config.get("type_folders") or {})

    template_files = dict(DEFAULT_TEMPLATE_FILES)
    template_files.update(config.get("template_files") or {})

    template_folder = str(config.get("template_folder", "_Templates"))
    exclude_folders = list(config.get("exclude_folders") or [".obsidian", "_backups", "_Templates"])

    target_path = resolve_target(vault_root, args.note)
    target = read_note(target_path, vault_root, type_folders)
    existing_notes = scan_vault(vault_root, target_path, type_folders, exclude_folders)
    alias_index = build_alias_index(existing_notes)

    print(f"Scanning vault: {vault_root}")
    print(f"Found {len(existing_notes)} other Markdown notes.")
    print(f"Calling Ollama model: {config.get('model', 'qwen3:1.7b')}")

    try:
        result = extract_entities(target, existing_notes, config)
    except urllib.error.URLError as exc:
        print(f"\nCould not reach Ollama: {exc}")
        print("Make sure Ollama is running and ollama_url in config.yaml is correct.")
        return 3
    except Exception as exc:
        print(f"\nOllama/extraction error: {exc}")
        return 3

    fuzzy_threshold = float(config.get("fuzzy_match_threshold", 0.88))

    cleaned_entities = []
    seen_entities = set()
    for e in result.get("entities", []):
        if not isinstance(e, dict):
            continue
        etype = str(e.get("type", "")).lower().strip()
        if etype not in ENTITY_TYPES:
            continue
        mention = str(e.get("mention") or e.get("name") or "").strip()
        name = str(e.get("name") or mention).strip()
        if not mention or not name:
            continue
        significance = str(e.get("significance") or "meaningful").lower().strip()
        if significance not in {"incidental", "meaningful", "recurring"}:
            significance = "meaningful"
        reason = str(e.get("reason") or "").strip()
        e = {
            "mention": mention,
            "name": name,
            "type": etype,
            "significance": significance,
            "reason": reason,
        }
        key = (normalize_name(mention), normalize_name(name), etype)
        if key in seen_entities:
            continue
        seen_entities.add(key)
        cleaned_entities.append(e)

    result["entities"] = cleaned_entities

    resolved_full: list[tuple[dict[str, Any], VaultNote | None, str]] = []
    for entity in cleaned_entities:
        note, method = resolve_entity(entity, alias_index, fuzzy_threshold)
        resolved_full.append((entity, note, method))

    type_map: dict[str, set[str]] = {}
    for entity in cleaned_entities:
        key = normalize_name(entity.get("mention") or entity.get("name") or "")
        if key:
            type_map.setdefault(key, set()).add(entity["type"])
    conflict_keys = {key for key, types in type_map.items() if len(types) > 1}

    create_missing = bool(config.get("create_missing_notes", True))
    update_reverse_links = bool(config.get("update_reverse_links", True))

    folder_problems = validate_type_folders(vault_root, type_folders, create_missing)
    if folder_problems:
        print("\nCONFIGURATION ERROR")
        print("The following configured entity folders do not exist:")
        for problem in folder_problems:
            print(f"  - {problem}")
        print("\nV4.2 will not create missing folders automatically.")
        print("Update type_folders in config.yaml to match your actual vault.")
        print("No vault changes were made.")
        return 2

    print_plan(target, resolved_full, create_missing, conflict_keys)

    campaign_state = clean_campaign_state(result)
    print_campaign_state(campaign_state)

    approved_new = select_new_entities(
        resolved_full,
        conflict_keys,
        create_missing=create_missing,
        auto_mode=bool(args.auto),
        dry_run=bool(args.dry_run),
    )

    if args.dry_run:
        print("\nDry run complete. No files changed.")
        return 0

    if not args.auto:
        answer = input("\nApply these changes? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. No files changed.")
            return 0

    backup_dir = vault_root / str(config.get("backup_folder", "_backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Backup the target before any mutation.
    target_backup = backup_file(target_path, vault_root, backup_dir)

    # Create missing notes first so they can be linked by canonical title.
    final_resolved: list[tuple[dict[str, Any], VaultNote | None]] = []
    created_paths: list[Path] = []

    for entity, note, method in resolved_full:
        entity_key = normalize_name(entity.get("mention") or entity.get("name") or "")
        if entity_key in conflict_keys:
            final_resolved.append((entity, None))
            continue

        if note is not None:
            final_resolved.append((entity, note))
            continue

        entity_type = str(entity.get("type") or "").lower()
        approval_key = (entity_key, entity_type)
        if approval_key not in approved_new:
            final_resolved.append((entity, None))
            continue

        if create_missing:
            new_path = create_entity_note(
                entity,
                vault_root,
                target.title,
                type_folders,
                template_folder,
                template_files,
            )
            created_paths.append(new_path)
            new_note = read_note(new_path, vault_root, type_folders)
            final_resolved.append((entity, new_note))
        else:
            final_resolved.append((entity, None))

    # Add links to the target body.
    replacements: list[tuple[str, str]] = []
    for entity, note in final_resolved:
        if note is None:
            continue
        for raw in (entity.get("mention"), entity.get("name")):
            if raw and normalize_name(raw):
                replacements.append((str(raw), note.title))

    new_body, changed_links = replace_mentions_with_links(target.body, replacements)
    new_fm = update_target_frontmatter(target.frontmatter, target, final_resolved)
    target_path.write_text(dump_markdown(new_fm, new_body), encoding="utf-8")

    reverse_changed: list[Path] = []
    if update_reverse_links:
        for entity, note in final_resolved:
            if note is None:
                continue
            # Newly created notes already receive the source link.
            if note.path in created_paths:
                continue
            backup_file(note.path, vault_root, backup_dir)
            if add_reverse_related_link(note.path, target.title):
                reverse_changed.append(note.path)

    print("\nApplied changes successfully.")
    print(f"Target backup: {target_backup.relative_to(vault_root)}")
    print(f"Target updated: {target.rel_path}")

    if changed_links:
        print("\nWikilinks added:")
        for mention, canonical in changed_links:
            print(f"  - {mention} -> [[{canonical}]]")

    if created_paths:
        print("\nCreated notes:")
        for path in created_paths:
            print(f"  - {path.relative_to(vault_root)}")

    if reverse_changed:
        print("\nRelated notes updated:")
        for path in reverse_changed:
            print(f"  - {path.relative_to(vault_root)}")

    print("\nTip: Ollama Notes Chat should re-index these changes automatically if auto-indexing is enabled.")
    return 0


def _format_runtime(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f} seconds"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {secs:.2f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m {secs:.2f}s"

def main() -> int:
    started = time.perf_counter()
    try:
        return _main_impl()
    finally:
        elapsed = time.perf_counter() - started
        print("\n" + "-" * 72)
        print(f"Total runtime: {_format_runtime(elapsed)}")
        print("-" * 72)

if __name__ == "__main__":
    raise SystemExit(main())
