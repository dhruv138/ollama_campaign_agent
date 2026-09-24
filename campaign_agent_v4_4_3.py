#!/usr/bin/env python3
"""
campaign_agent_v4_3.py
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

    generic_object_noise = {
        "job board", "the job board", "flyer", "the flyer",
        "robes", "the robes", "red robes", "the red robes",
        "tattoo", "the tattoo", "skeletal hand tattoo", "the skeletal hand tattoo",
    }
    if key in generic_object_noise:
        return "blocked", "generic object/detail; keep as evidence rather than standalone note"

    if etype == "item" and key in {"gala", "the gala"}:
        return "blocked", "event/objective appears misclassified as an item"

    if etype == "item" and ("tattoo" in key or key.endswith(" tattoo")):
        return "blocked", "clue/detail rather than a durable standalone item"

    if etype == "faction" and key in {"gollath", "goliath", "the gollath", "the goliath"}:
        return "blocked", "likely demographic/descriptor misclassified as faction"

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

    if etype == "lore":
        return "review", "lore candidate; report-only until Lore templates/folders are finalized"

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


def _parse_ollama_json(raw: str, target: VaultNote, label: str) -> dict[str, Any]:
    """Strict JSON parsing with debug capture; never silently repair malformed model output."""
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
            safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)
            debug_path = debug_dir / f"ollama_failed_{safe_label}_{stamp}.txt"
            debug_path.write_text(raw, encoding="utf-8")
            lines = raw.splitlines()
            lo = max(0, parse_error.lineno - 4)
            hi = min(len(lines), parse_error.lineno + 3)
            nearby = "\n".join(f"{i + 1:>6}: {lines[i]}" for i in range(lo, hi))
            raise ValueError(
                f"Ollama returned malformed JSON during {label}.\n"
                f"Parse failure: line {parse_error.lineno}, column {parse_error.colno}, "
                f"char {parse_error.pos}.\n"
                f"Raw response length: {len(raw):,} characters.\n"
                f"Raw response saved to: {debug_path}\n"
                "Nearby Ollama output:\n"
                "----------------------------------------\n"
                f"{nearby}\n"
                "----------------------------------------"
            ) from parse_error
    return result if isinstance(result, dict) else {}


def _focused_pass(
    target: VaultNote,
    config: dict[str, Any],
    label: str,
    instructions: str,
    shape: str,
) -> dict[str, Any]:
    prompt = f"""
You are extracting durable knowledge from one D&D campaign session for an Obsidian vault.

TASK: {label}

{instructions}

STRICT RULES
- Use ONLY information explicitly supported by the session note.
- Be inclusive: a fact can be important even if mentioned only once.
- Preserve uncertainty. A suspicion, rumor, or question is NOT an established fact.
- Do not add generic D&D lore or outside knowledge.
- Deduplicate within this pass.
- Keep details concise but preserve distinctive evidence.
- Output JSON only, exactly matching the requested shape.

REQUESTED JSON SHAPE
{shape}

SESSION NOTE
============
{target.body}
"""
    print(f"  → {label}")
    raw = ollama_chat(
        config.get("ollama_url", "http://localhost:11434"),
        config.get("model", "qwen3:1.7b"),
        prompt,
        float(config.get("temperature", 0.1)),
        int(config.get("ollama_timeout_seconds", 120)),
    )
    return _parse_ollama_json(raw, target, label)


def _extract_explicit_todos(body: str) -> list[dict[str, Any]]:
    """Deterministically parse every bullet after a Todo list heading."""
    lines = body.splitlines()
    todos: list[dict[str, Any]] = []
    in_todos = False
    current: str | None = None

    def flush() -> None:
        nonlocal current
        if current and current.strip():
            detail = re.sub(r"\s+", " ", current).strip()
            # Strip wikilink markup only for the display title; keep the detail intact.
            title = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
                           lambda m: (m.group(2) or m.group(1)), detail)
            done = bool(re.search(r"\(\s*done\s*\)\s*$", title, flags=re.I))
            if done:
                title = re.sub(r"\s*\(\s*done\s*\)\s*$", "", title, flags=re.I).strip()
                detail = re.sub(r"\s*\(\s*done\s*\)\s*$", "", detail, flags=re.I).strip()
            todos.append({
                "title": title,
                "detail": detail,
                "related": [],
                "status": "completed" if done else "planned",
                "source": "explicit-todo",
            })
        current = None

    for line in lines:
        stripped = line.strip()
        if not in_todos:
            if re.match(r"^(?:#+\s*)?todo(?:\s+list)?\s*:?\s*$", stripped, flags=re.I):
                in_todos = True
            continue

        # A new Markdown heading ends the Todo section.
        if re.match(r"^#{1,6}\s+\S", stripped):
            flush()
            break

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            flush()
            current = bullet.group(1).strip()
            continue

        # Blank lines don't terminate the section. Indented/non-bullet text after
        # a bullet is treated as a wrapped continuation.
        if stripped and current is not None:
            current += " " + stripped

    flush()
    return todos




def read_canonical_session_body(target: VaultNote) -> str:
    """Read the target Markdown directly from disk and strip leading YAML frontmatter."""
    raw = target.path.read_text(encoding="utf-8").lstrip("\ufeff")
    if raw.startswith("---"):
        lines = raw.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                raw = "".join(lines[i + 1:])
                break
    return raw


def deterministic_source_diagnostics(body: str) -> dict[str, int]:
    return {
        "chars": len(body),
        "wikilinks": len(re.findall(r"\[\[[^\]]+\]\]", body)),
        "todo_markers": len(re.findall(r"(?im)^\s*(?:#+\s*)?todo(?:\s+list)?\s*:?\s*$", body)),
        "torm": len(re.findall(r"\bTorm\b", body, re.I)),
        "blond": len(re.findall(r"\bblond(?:e)?\w*\b", body, re.I)),
        "two_tits": len(re.findall(r"Two Tits Inn", body, re.I)),
    }



def _yaml_entity_values(target: VaultNote) -> list[tuple[str, str]]:
    """Return entity-like values already present in session YAML, without requiring them."""
    key_types = {
        "npcs": "npc", "npc": "npc",
        "locations": "location", "location": "location",
        "factions": "faction", "faction": "faction",
        "quests": "quest", "quest": "quest",
        "items": "item", "item": "item",
    }
    out: list[tuple[str, str]] = []
    fm = target.frontmatter if isinstance(target.frontmatter, dict) else {}
    for key, etype in key_types.items():
        value = fm.get(key)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                # YAML may itself contain [[wikilinks]].
                clean = re.sub(r"^\[\[|\]\]$", "", item.strip())
                clean = clean.split("|", 1)[0].strip()
                if clean:
                    out.append((clean, etype))
    return out


def extract_yaml_entities(target: VaultNote, alias_index: dict[str, list[VaultNote]]) -> list[dict[str, Any]]:
    """Resolve existing session YAML against the vault. YAML is optional, not required input."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_name, expected_type in _yaml_entity_values(target):
        candidates = alias_index.get(normalize_name(raw_name), [])
        typed = [n for n in candidates if str(n.note_type or "").lower().strip() == expected_type]
        candidates = typed or candidates
        if len(candidates) != 1:
            continue
        note = candidates[0]
        etype = str(note.note_type or "").lower().strip()
        if etype not in ENTITY_TYPES:
            continue
        key = (normalize_name(note.title), etype)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "mention": raw_name,
            "name": note.title,
            "type": etype,
            "significance": "meaningful",
            "reason": "existing note resolved from session YAML",
            "from_yaml": True,
        })
    return rows


def extract_prose_existing_entities(
    body: str,
    existing_notes: list[VaultNote],
) -> list[dict[str, Any]]:
    """
    Deterministically match existing note titles/aliases in prose.
    Conservative boundaries prevent substring matches inside larger words.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    lower_body = body.casefold()

    for note in existing_notes:
        etype = str(note.note_type or "").lower().strip()
        if etype not in ENTITY_TYPES:
            continue

        names: set[str] = {str(note.title).strip()}
        fm = note.frontmatter if isinstance(note.frontmatter, dict) else {}
        for key in ("name", "title"):
            v = fm.get(key)
            if isinstance(v, str) and v.strip():
                names.add(v.strip())
        aliases = fm.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            names.update(str(x).strip() for x in aliases if str(x).strip())

        # Prefer longer names first; reject very short/generic aliases.
        usable = sorted(
            {n for n in names if len(normalize_name(n)) >= 3},
            key=len,
            reverse=True,
        )
        matched = None
        for candidate in usable:
            # Flexible whitespace, but literal punctuation.
            parts = re.split(r"\s+", candidate.strip())
            pat = r"(?<![\w])" + r"\s+".join(re.escape(p) for p in parts) + r"(?![\w])"
            m = re.search(pat, body, re.I)
            if m:
                matched = m.group(0)
                break
        if not matched:
            continue

        key = (normalize_name(note.title), etype)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "mention": matched,
            "name": note.title,
            "type": etype,
            "significance": "meaningful",
            "reason": "existing note name/alias found verbatim in session prose",
            "from_prose": True,
        })
    return rows


def _logical_source_blocks(body: str) -> list[str]:
    """Join wrapped prose lines while preserving bullets/headings as block boundaries."""
    blocks: list[str] = []
    current: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            continue
        if re.match(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", line):
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            blocks.append(line)
        else:
            current.append(line)
    if current:
        blocks.append(" ".join(current).strip())
    return blocks


def extract_explicit_questions_v443(body: str) -> list[dict[str, Any]]:
    """Extract only explicit source suspicions/questions from logical wrapped blocks."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in _logical_source_blocks(body):
        if not (
            "?" in block
            or re.search(r"\b(?:we think|we suspect|wonder(?:ing)? if|could be|connected to)\b", block, re.I)
        ):
            continue

        # Require uncertainty/suspicion rather than arbitrary dialogue questions.
        if not re.search(r"\b(?:we think|we suspect|wonder(?:ing)? if|could be|connected to)\b|\?!", block, re.I):
            continue

        detail = re.sub(r"\s+", " ", block).strip()
        low = detail.casefold()
        if "daggins" in low and "connected" in low:
            title = "Are the Two Tits Inn basement gatherings connected to Daggins' Order?"
        elif "blond" in low and ("gollath" in low or "goliath" in low or "skeletal hand" in low):
            title = "Is the Blond-Haired Youth connected to the earlier Skeletal Hand encounter?"
        else:
            # Source-derived fallback, deliberately not an AI-generated interpretation.
            title = detail[:177] + ("..." if len(detail) > 177 else "")

        key = normalize_name(title)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "title": title,
            "detail": detail,
            "related": [],
            "status": "unresolved|explicit-source",
            "evidence": [detail],
            "source": "explicit-source",
        })
    return rows


def attach_evidence_to_state_v443(result: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    """
    Conservative evidence attachment. Require >=2 meaningful token overlaps, or one
    distinctive long token. This avoids unrelated robe evidence attaching to quests.
    """
    stop = {
        "the","and","that","this","with","from","they","them","their","there","into",
        "about","have","has","had","was","were","for","but","not","who","what","when",
        "where","which","session","established","observed","active","unresolved"
    }
    def tokens(s: str) -> set[str]:
        return {x for x in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", s.casefold()) if x not in stop}

    for section in ("quests", "clues", "developments", "rumors", "open_questions"):
        for row in result.get(section, []) or []:
            if not isinstance(row, dict):
                continue
            query = " ".join(str(row.get(k, "")) for k in ("title", "detail", "name", "description"))
            qt = tokens(query)
            scored: list[tuple[int, dict[str, Any]]] = []
            for ev in evidence:
                et = tokens(str(ev.get("source", "")))
                overlap = qt & et
                score = len(overlap)
                distinctive = any(len(t) >= 9 for t in overlap)
                if score >= 2 or distinctive:
                    scored.append((score, ev))
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                row["evidence"] = [x[1]["source"] for x in scored[:2]]


def extract_wikilink_entities(
    target: VaultNote,
    existing_notes: list[VaultNote],
    alias_index: dict[str, list[VaultNote]] | None = None,
    source_body: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve literal [[wikilinks]] deterministically; model output is not involved."""
    if alias_index is None:
        alias_index = build_alias_index(existing_notes)

    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    pattern = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")

    body = source_body if source_body is not None else target.body
    for match in pattern.finditer(body):
        link_target = match.group(1).strip()
        display = (match.group(2) or link_target).strip()
        candidates = alias_index.get(normalize_name(link_target), [])
        if not candidates:
            continue

        # A literal wikilink is authoritative only when it resolves unambiguously.
        note = candidates[0] if len(candidates) == 1 else next(
            (n for n in candidates if normalize_name(n.title) == normalize_name(link_target)),
            None,
        )
        if note is None:
            continue

        etype = str(note.note_type or "").lower().strip()
        if etype not in ENTITY_TYPES:
            continue

        key = (normalize_name(note.title), etype)
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "mention": display,
            "name": note.title,
            "type": etype,
            "significance": "meaningful",
            "reason": "literal wikilink resolved from session source",
            "from_wikilink": True,
        })
    return found


def promote_clue_people(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Promote distinctive unnamed people from clue/mystery evidence to review."""
    promoted: list[dict[str, Any]] = []
    seen: set[str] = set()
    texts: list[str] = []

    for category in ("clues", "rumors", "open_questions"):
        for row in result.get(category, []):
            if isinstance(row, dict):
                texts.append(
                    f"{row.get('title') or ''} {row.get('detail') or ''}".strip()
                )

    patterns = [
        (
            r"\b(?:young man|youth)\s+with\s+blond(?:e)?\s+hair\b|\bblond(?:e)?[- ]haired\s+(?:young man|youth)\b|\bblond youth\b",
            "Blond-Haired Youth",
        ),
        (r"\bblack[- ]haired woman\b", "Black-Haired Woman"),
    ]

    for text in texts:
        for pattern, stable_name in patterns:
            if not re.search(pattern, text, flags=re.I):
                continue
            key = normalize_name(stable_name)
            if key in seen:
                continue
            seen.add(key)
            recurring = any(
                token in text.lower()
                for token in ("amberhold", "speak with dead", "copied his tattoo", "earlier")
            )
            promoted.append({
                "mention": stable_name,
                "name": stable_name,
                "type": "npc",
                "significance": "recurring" if recurring else "meaningful",
                "reason": "distinctive unnamed person surfaced by clue/mystery extraction",
                "promoted_from_clue": True,
            })
    return promoted



def extract_source_evidence(body: str) -> list[dict[str, Any]]:
    """Extract verbatim source windows around high-value patterns."""
    lines = body.splitlines()
    rules = [
        ("blond_recruiter", re.compile(r"blond(?:e)?|young man with blond", re.I)),
        ("torm_sect", re.compile(r"\bTorm\b", re.I)),
        ("island", re.compile(r"unchartable island|unplottable island|island in the distance|Barren Toll|Wellview Helm", re.I)),
        ("academy_robes", re.compile(r"\brobes?\b|\bred\b.*\bpurple\b|\bpurple\b.*\bred\b", re.I)),
        ("black_feathers", re.compile(r"Black Feathers", re.I)),
        ("two_tits_basement", re.compile(r"Two Tits Inn|basement|phallic pins|Daggins.? Order", re.I)),
    ]

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        for kind, pattern in rules:
            if not pattern.search(line):
                continue
            lo, hi = max(0, i - 1), min(len(lines), i + 2)
            excerpt = "\n".join(x.rstrip() for x in lines[lo:hi] if x.strip()).strip()
            key = (kind, excerpt)
            if excerpt and key not in seen:
                seen.add(key)
                rows.append({"kind": kind, "source": excerpt})
    return rows


def extract_explicit_questions(body: str) -> list[dict[str, Any]]:
    """
    Only preserve questions/suspicions explicitly present in the source.
    Qwen is not allowed to invent OPEN QUESTIONS in V4.4.1.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in body.splitlines():
        line = raw_line.strip().lstrip("-* ").strip()
        if not line:
            continue
        explicit = (
            "?" in line
            or re.search(r"\b(?:we think|we suspect|wonder(?:ing)? if|could this|are they connected)\b", line, re.I)
        )
        if not explicit:
            continue
        # Avoid treating routine dialogue/questions as campaign mysteries unless
        # uncertainty/suspicion language or an emphatic written question is present.
        if "?" in line or re.search(r"\b(?:we think|we suspect|wonder(?:ing)? if|could this|connected)\b", line, re.I):
            title = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
                           lambda m: (m.group(2) or m.group(1)), line)
            title = re.sub(r"\s+", " ", title).strip()
            key = normalize_name(title)
            if key and key not in seen:
                seen.add(key)
                rows.append({
                    "title": title[:180],
                    "detail": line,
                    "related": [],
                    "status": "unresolved|explicit-source",
                    "evidence": [line],
                    "source": "explicit-source",
                })
    return rows


def entities_from_raw_evidence(body: str) -> list[dict[str, Any]]:
    """Conservative entity candidates derived directly from session text."""
    rows = []
    if re.search(r"\bblond youth\b|\byoung man with blond(?:e)? hair\b|\bblond(?:e)?[- ]haired youth\b", body, re.I):
        recurring = bool(re.search(r"Amberhold", body, re.I) and re.search(r"Skeletal Hand", body, re.I))
        rows.append({
            "mention": "Blond-Haired Youth", "name": "Blond-Haired Youth",
            "type": "npc", "significance": "recurring" if recurring else "meaningful",
            "reason": "raw session text describes a distinctive blond youth connected to Skeletal Hand evidence",
        })
    if re.search(r"\bblack[- ]haired woman\b", body, re.I):
        rows.append({
            "mention": "Black-Haired Woman", "name": "Black-Haired Woman",
            "type": "npc", "significance": "meaningful",
            "reason": "raw session text describes a distinctive unidentified black-haired woman",
        })
    if re.search(r"\bTorm\b", body):
        rows.append({
            "mention": "Torm", "name": "Torm", "type": "lore",
            "significance": "meaningful",
            "reason": "deity explicitly named in raw session text",
        })
    return rows


def attach_evidence_to_state(result: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    """Attach raw excerpts to related AI interpretations for human review."""
    keywords = {
        "blond_recruiter": ("blond", "skeletal hand", "tattoo", "recruit"),
        "torm_sect": ("torm", "religious sect", "alcohol"),
        "island": ("island", "barren toll", "wellview"),
        "academy_robes": ("robe", "red", "purple"),
        "black_feathers": ("black feathers", "raven queen", "matron"),
        "two_tits_basement": ("two tits", "basement", "daggins", "fertility"),
    }
    for category in ("quests", "clues", "developments", "rumors", "open_questions"):
        for row in result.get(category, []):
            if not isinstance(row, dict):
                continue
            hay = f"{row.get('title','')} {row.get('detail','')}".lower()
            matches = []
            for ev in evidence:
                if any(term in hay for term in keywords.get(ev["kind"], ())):
                    matches.append(ev["source"])
            if matches:
                row["evidence"] = list(dict.fromkeys(matches))[:2]


def extract_entities(
    target: VaultNote,
    existing_notes: list[VaultNote],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    V4.3 uses several small focused passes. Qwen observes; Python resolves,
    deduplicates, parses explicit TODOs, and makes all vault-write decisions.
    """
    print("Running focused extraction passes:")

    entities = _focused_pass(
        target, config, "Entities",
        """
Extract NEW OR UNLINKED durable entity candidates.
Existing Obsidian [[wikilinks]] are resolved separately by Python, so do not spend
output repeating obvious linked entities unless they are necessary to identify a new clue.
- npc: named people/creatures; ALSO unnamed individuals when they perform a meaningful
  action, carry a clue, or are explicitly connected to an earlier event/appearance.
- location: specifically named places.
- faction: specifically named organizations/orders/sects.
- quest: specifically named missions only when the text treats them as a durable concept.
- item: specifically named/notable objects.
- lore: named deities, religions, historical concepts, magical/world concepts, or other
  durable named lore that does not fit the above.

For every entity include significance:
incidental | meaningful | recurring.
Recurring means the SESSION ITSELF connects the entity to an earlier event/appearance.
Do not call something recurring merely because it appears twice in this same note.
For unnamed meaningful people, use a stable descriptive name such as "Blond-Haired Youth".
""",
        """{
  "entities": [
    {
      "mention": "...",
      "name": "...",
      "type": "npc|location|faction|quest|item|lore",
      "significance": "incidental|meaningful|recurring",
      "reason": "brief evidence"
    }
  ]
}"""
    )

    objectives = _focused_pass(
        target, config, "Quests and Objectives",
        """
Extract accepted/offered missions, investigations, and durable objectives.
Do NOT reproduce the explicit Todo list; Python parses that section directly.
Distinguish active, offered, completed, and unresolved objectives.
""",
        """{
  "quests": [
    {"title":"...", "detail":"...", "related":[], "status":"active|offered|completed|unresolved"}
  ]
}"""
    )

    clues = _focused_pass(
        target, config, "Clues and Mysteries",
        """
Inspect EACH SCENE for:
- concrete observations/evidence,
- suspicious behavior,
- unexplained phenomena,
- connections to earlier events stated by the session,
- rumors/reported claims,
- unresolved questions or suspicions.

Classify each entry as clue, rumor, or open_question.
Do not collapse several distinct observations into a vague summary.
Do not turn a suspicion into a fact.
""",
        """{
  "clues": [
    {"title":"...", "detail":"...", "related":[], "status":"observed"}
  ],
  "rumors": [
    {"title":"...", "detail":"...", "related":[], "status":"reported"}
  ],
  "open_questions": [
    {"title":"...", "detail":"...", "related":[], "status":"unresolved|suspected"}
  ]
}"""
    )

    lore = _focused_pass(
        target, config, "Lore and World Knowledge",
        """
Inspect EACH SCENE for durable world knowledge, even when mentioned only once:
- deities and religions,
- organizations/orders/sects,
- historical facts and timelines,
- customs/traditions,
- magical or world rules,
- named historical events,
- unexplained world phenomena,
- new facts about existing people, places, factions, institutions, or lore.

Examples of the KIND of thing to notice (do not invent them): a deity being named,
a sect's behavior, an institution changing uniforms decades ago, an order's doctrine,
or a strange recurring geographic phenomenon.

Return developments only when the session actually establishes/reports them.
""",
        """{
  "developments": [
    {"title":"...", "detail":"...", "related":[], "status":"established|observed|reported"}
  ],
  "lore_entities": [
    {
      "mention":"...",
      "name":"...",
      "type":"lore",
      "subtype":"deity|religion|history|phenomenon|concept|other",
      "significance":"meaningful|recurring",
      "reason":"brief evidence"
    }
  ]
}"""
    )

    # V4.4.2: all deterministic extraction uses one canonical raw source.
    canonical_body = read_canonical_session_body(target)
    source_diag = deterministic_source_diagnostics(canonical_body)

    alias_index = build_alias_index(existing_notes)
    yaml_entities = extract_yaml_entities(target, alias_index)
    wikilink_entities = extract_wikilink_entities(
        target, existing_notes, alias_index, canonical_body
    )
    prose_entities = extract_prose_existing_entities(canonical_body, existing_notes)

    combined_entities = []
    combined_entities.extend(yaml_entities)
    combined_entities.extend(wikilink_entities)
    combined_entities.extend(prose_entities)
    combined_entities.extend(entities.get("entities", []))
    combined_entities.extend(lore.get("lore_entities", []))

    source_questions = extract_explicit_questions_v443(canonical_body)
    preliminary = {
        "clues": clues.get("clues", []),
        "rumors": clues.get("rumors", []),
        "open_questions": source_questions,
    }
    combined_entities.extend(promote_clue_people(preliminary))
    combined_entities.extend(entities_from_raw_evidence(canonical_body))

    evidence = extract_source_evidence(canonical_body)
    result = {
        "entities": combined_entities,
        "quests": objectives.get("quests", []),
        "clues": clues.get("clues", []),
        "developments": lore.get("developments", []),
        "rumors": clues.get("rumors", []),
        # Source-only by design. Never substitute Qwen-generated questions here.
        "open_questions": source_questions,
        "todos": _extract_explicit_todos(canonical_body),
        "relationships": [],
        "source_evidence": evidence,
        "source_diagnostics": source_diag,
    }
    attach_evidence_to_state_v443(result, evidence)
    result["entity_source_diagnostics"] = {
        "yaml": len(yaml_entities),
        "wikilinks": len(wikilink_entities),
        "prose": len(prose_entities),
        "unique_deterministic": len({
            (normalize_name(e.get("name", "")), e.get("type", ""))
            for e in (yaml_entities + wikilink_entities + prose_entities)
        }),
    }
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


def _fact_fingerprint(text: str) -> set[str]:
    stop = {
        "the","a","an","and","or","to","of","in","on","at","for","with","that","this",
        "they","he","she","it","was","were","is","are","be","been","by","from","as",
        "their","his","her","we","us","our","said","says"
    }
    return {
        w for w in re.findall(r"[a-z0-9']+", text.lower())
        if len(w) > 2 and w not in stop
    }


def _too_similar_fact(a: str, b: str) -> bool:
    aa, bb = _fact_fingerprint(a), _fact_fingerprint(b)
    if not aa or not bb:
        return normalize_name(a) == normalize_name(b)
    overlap = len(aa & bb) / max(1, min(len(aa), len(bb)))
    return overlap >= 0.78


def clean_campaign_state(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    cleaned: dict[str, list[dict[str, Any]]] = {}
    global_details: list[str] = []
    # Priority means a concrete clue wins over a duplicate development/question.
    for category in ("quests", "clues", "rumors", "open_questions", "developments", "todos"):
        rows: list[dict[str, Any]] = []
        for raw in result.get(category, []):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            detail = str(raw.get("detail") or "").strip()
            if not title or not detail:
                continue
            # Explicit TODOs are allowed to overlap quests; that's intentional.
            duplicate = False
            if category != "todos":
                duplicate = any(_too_similar_fact(detail, prior) for prior in global_details)
            if duplicate:
                continue
            related = [str(x).strip() for x in as_list(raw.get("related")) if str(x).strip()]
            row = {
                "title": title,
                "detail": detail,
                "related": related,
                "status": str(raw.get("status") or "").strip(),
                "evidence": [str(x).strip() for x in as_list(raw.get("evidence")) if str(x).strip()],
            }
            rows.append(row)
            if category != "todos":
                global_details.append(detail)
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
            for excerpt in row.get("evidence", []):
                shown = excerpt if len(excerpt) <= 420 else excerpt[:417] + "..."
                print(f"    SOURCE: {shown}")
    if not any_rows:
        print("  (none)")
    print("\nNOTE: Campaign-state and lore sections are report-only in V4.4.3.")
    print("Model-written details are interpretations; OPEN QUESTIONS are source-only in V4.4.3 and all AI details must be reviewed")
    print("against the source before any future canon UPDATE operation.")
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
    required_entity_types = {"npc", "location", "faction", "quest", "item", "session"}
    for etype, folder in type_folders.items():
        if etype not in required_entity_types:
            continue
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
    print("CAMPAIGN AGENT V4.4.3 PLAN")
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
        corrected = ""
        if entity.get("model_type") and entity.get("model_type") != entity.get("type"):
            corrected = f", model said {entity.get('model_type')}"
        print(f"  ✓ {entity.get('mention')!r} -> [[{note.title}]] "
              f"[{entity.get('type')}, {method}{corrected}]")

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
        if status == "blocked" or etype == "lore":
            continue

        label = "NEW ENTITY" if status == "eligible" else "REVIEW CANDIDATE"
        print(f"\n{label}: {mention}")
        print(f"Type: {etype}")
        if status == "review":
            print(f"Reason for review: {reason}")
        if ask_yes_no(f"Create [[{entity.get('name') or mention}]]?"):
            approved.add(key)

    return approved


def normalize_type_folder_config(raw: Any) -> dict[str, str]:
    aliases = {
        "npc": "npc", "npcs": "npc", "people": "npc",
        "location": "location", "locations": "location", "places": "location",
        "faction": "faction", "factions": "faction",
        "quest": "quest", "quests": "quest",
        "item": "item", "items": "item",
        "session": "session", "sessions": "session",
        "campaign": "campaign",
        "clue": "clues", "clues": "clues",
        "lore": "lore",
    }
    result = dict(DEFAULT_TYPE_FOLDERS)
    if not isinstance(raw, dict):
        return result
    unknown = []
    for key, value in raw.items():
        normalized_key = aliases.get(str(key).strip().lower())
        if not normalized_key:
            unknown.append(str(key))
            continue
        value = str(value).strip().strip("/\\")
        if value:
            result[normalized_key] = value
    if unknown:
        print("Warning: unrecognized type_folders keys ignored: " + ", ".join(unknown))
    return result


def print_effective_paths(config_path: Path, vault_root: Path, type_folders: dict[str, str]) -> None:
    print(f"Config file: {config_path}")
    print(f"Vault root:  {vault_root}")
    print("Effective entity folders:")
    for etype in ("npc", "location", "faction", "quest", "item", "session", "campaign", "clues", "lore"):
        folder = type_folders.get(etype)
        exists = bool(folder) and (vault_root / folder).is_dir()
        print(f"  {'✓' if exists else '✗'} {etype:<8} -> {folder}/")



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

    type_folders = normalize_type_folder_config(config.get("type_folders"))
    create_missing = bool(config.get("create_missing_notes", True))
    print_effective_paths(config_path, vault_root, type_folders)

    folder_problems = validate_type_folders(vault_root, type_folders, create_missing)
    if folder_problems:
        print("\nCONFIGURATION ERROR")
        print("The following configured entity folders do not exist under the vault root:")
        for problem in folder_problems:
            print(f"  - {problem}")
        print("\nNo Ollama calls were made and no vault changes were made.")
        print("Check the Config file and Vault root paths printed above.")
        return 2

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

        # Collapse multiple model phrasings for the same unidentified sailor.
        if etype == "npc" and re.search(r"goll?i?ath.*sailor|goliath.*sailor", mention, re.I):
            name = "Goliath Sailor"
            mention = "Goliath Sailor"
            if not reason:
                reason = "descriptive unnamed sailor reference; spelling normalized for candidate deduplication"

        e = {
            "mention": mention,
            "name": name,
            "type": etype,
            "significance": significance,
            "reason": reason,
        }
        key = (normalize_name(name), etype)
        if key in seen_entities:
            continue
        seen_entities.add(key)
        cleaned_entities.append(e)

    result["entities"] = cleaned_entities

    resolved_full: list[tuple[dict[str, Any], VaultNote | None, str]] = []
    for entity in cleaned_entities:
        note, method = resolve_entity(entity, alias_index, fuzzy_threshold)
        if note is not None and note.note_type:
            # Existing vault metadata is canonical. Qwen may misclassify WISC, Raven Queen, etc.
            canonical_type = str(note.note_type).lower().strip()
            if canonical_type:
                entity["model_type"] = entity.get("type")
                entity["type"] = canonical_type
        resolved_full.append((entity, note, method))

    type_map: dict[str, set[str]] = {}
    for entity in cleaned_entities:
        key = normalize_name(entity.get("mention") or entity.get("name") or "")
        if key:
            type_map.setdefault(key, set()).add(entity["type"])
    conflict_keys = {key for key, types in type_map.items() if len(types) > 1}

    update_reverse_links = bool(config.get("update_reverse_links", True))

    print_plan(target, resolved_full, create_missing, conflict_keys)

    campaign_state = clean_campaign_state(result)
    print_campaign_state(campaign_state)

    entity_diag = result.get("entity_source_diagnostics", {})
    if entity_diag:
        print("\nDETERMINISTIC ENTITY DISCOVERY")
        print(f"  YAML metadata : {entity_diag.get('yaml', 0)}")
        print(f"  Wikilinks     : {entity_diag.get('wikilinks', 0)}")
        print(f"  Prose matches : {entity_diag.get('prose', 0)}")
        print(f"  Unique notes  : {entity_diag.get('unique_deterministic', 0)}")

    diag = result.get("source_diagnostics", {})
    if diag:
        print("\nCANONICAL SOURCE DIAGNOSTICS")
        print(f"  characters : {diag.get('chars', 0)}")
        print(f"  wikilinks  : {diag.get('wikilinks', 0)}")
        print(f"  todo header: {diag.get('todo_markers', 0)}")
        print(f"  Torm hits  : {diag.get('torm', 0)}")
        print(f"  blond hits : {diag.get('blond', 0)}")
        print(f"  Two Tits   : {diag.get('two_tits', 0)}")

    evidence_rows = result.get("source_evidence", [])
    print(f"\nDeterministic source evidence windows: {len(evidence_rows)}")
    if evidence_rows:
        print("\nSOURCE EVIDENCE INDEX (deterministic; report-only)")
        for ev in evidence_rows:
            shown = ev["source"] if len(ev["source"]) <= 300 else ev["source"][:297] + "..."
            print(f"  [{ev['kind']}] {shown}")
    else:
        print("  WARNING: no evidence windows matched; source grounding needs review.")

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
