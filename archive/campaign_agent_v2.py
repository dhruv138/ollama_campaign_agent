#!/usr/bin/env python3
"""
campaign_agent_v2.py
Manual Obsidian campaign-note processor using a local Ollama model.

Features
--------
- Processes one Markdown note at a time.
- Scans the vault for existing NPC/location/faction/quest/item/session notes.
- Uses Ollama to extract campaign entities + relationships from the target note.
- Resolves extracted names against existing note names and aliases.
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
    python3 campaign_agent_v2.py "Sessions/Session 38.md" --dry-run
    python3 campaign_agent_v2.py "Sessions/Session 38.md"
    python3 campaign_agent_v2.py "Sessions/Session 38.md" --auto
"""

from __future__ import annotations

import argparse
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
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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
        names = [note.title, note.path.stem, *note.aliases]
        for name in names:
            key = normalize_name(name)
            if key:
                index.setdefault(key, []).append(note)
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
    catalog: dict[str, list[str]] = {t: [] for t in ENTITY_TYPES}
    for note in existing_notes:
        if note.note_type in catalog:
            aliases = f" (aliases: {', '.join(note.aliases)})" if note.aliases else ""
            catalog[note.note_type].append(f"{note.title}{aliases}")

    max_catalog_per_type = int(config.get("max_catalog_per_type", 500))
    catalog_text = []
    for t in ENTITY_TYPES:
        vals = catalog[t][:max_catalog_per_type]
        catalog_text.append(f"{t.upper()}:\n" + "\n".join(f"- {v}" for v in vals))

    prompt = f"""
Analyze the campaign note below.

Identify explicit references to:
- NPCs
- locations
- factions
- quests
- items

Also identify explicit relationships stated in the note.

Important rules:
- Use only facts stated in the note.
- Do not invent D&D lore.
- Do not create an entity merely because a common noun is capitalized.
- Prefer a canonical entity name from the EXISTING VAULT CATALOG when it is clearly the same entity.
- If the note uses an alias or shortened form, put the text that appears in the note in "mention".
- If an entity appears to be new, set "existing_name" to null.
- A relationship should be a short factual statement from this note.

Return JSON exactly in this shape:
{{
  "entities": [
    {{
      "mention": "text as written in the note",
      "name": "best canonical name",
      "type": "npc|location|faction|quest|item",
      "existing_name": "canonical existing note name or null",
      "confidence": 0.0
    }}
  ],
  "relationships": [
    {{
      "subject": "entity name",
      "relation": "short relationship verb/phrase",
      "object": "entity name",
      "confidence": 0.0
    }}
  ]
}}

EXISTING VAULT CATALOG
======================
{chr(10).join(catalog_text)}

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
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise ValueError(f"Ollama did not return valid JSON:\n{raw}")
        result = json.loads(match.group(0))

    result.setdefault("entities", [])
    result.setdefault("relationships", [])
    return result


def resolve_entity(
    entity: dict[str, Any],
    alias_index: dict[str, list[VaultNote]],
    fuzzy_threshold: float,
) -> tuple[VaultNote | None, str]:
    candidates = [
        entity.get("existing_name"),
        entity.get("name"),
        entity.get("mention"),
    ]

    for value in candidates:
        if not value:
            continue
        key = normalize_name(value)
        hits = alias_index.get(key, [])
        if len(hits) == 1:
            return hits[0], "exact"
        if len(hits) > 1:
            etype = entity.get("type")
            typed = [n for n in hits if n.note_type == etype]
            if len(typed) == 1:
                return typed[0], "exact-type"

    key = normalize_name(entity.get("name") or entity.get("mention") or "")
    if not key:
        return None, "none"

    possible_keys = list(alias_index.keys())
    close = difflib.get_close_matches(key, possible_keys, n=1, cutoff=fuzzy_threshold)
    if close:
        hits = alias_index[close[0]]
        etype = entity.get("type")
        typed = [n for n in hits if n.note_type == etype]
        if len(typed) == 1:
            return typed[0], "fuzzy"
        if len(hits) == 1:
            return hits[0], "fuzzy"

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
        canonical = note.title if note else str(entity.get("name") or entity.get("mention") or "").strip()
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
            "Created by campaign_agent_v2.py. Review and expand this note.\n\n"
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


def print_plan(
    target: VaultNote,
    result: dict[str, Any],
    resolved: list[tuple[dict[str, Any], VaultNote | None, str]],
    create_missing: bool,
    update_reverse_links: bool,
    conflict_keys: set[str] | None = None,
) -> None:
    print("\n" + "=" * 72)
    print("CAMPAIGN AGENT PLAN")
    print("=" * 72)
    print(f"Target: {target.rel_path}\n")

    conflict_keys = conflict_keys or set()
    existing, new_entities, conflicts = [], [], []
    for entity, note, method in resolved:
        key = normalize_name(entity.get("mention") or entity.get("name") or "")
        if key in conflict_keys:
            conflicts.append((entity, note, method))
        elif note:
            existing.append((entity, note, method))
        else:
            new_entities.append((entity, note, method))

    print("EXISTING MATCHES")
    if not existing:
        print("  (none)")
    for entity, note, method in existing:
        conf = "unspecified" if entity.get("_confidence_unspecified") else entity.get("confidence")
        print(f"  ✓ {entity.get('mention')!r} -> [[{note.title}]] [{entity.get('type')}, {method}, conf={conf}]")

    print("\nNEW / UNRESOLVED ENTITIES")
    if not new_entities:
        print("  (none)")
    for entity, _, method in new_entities:
        action = "proposed CREATE" if create_missing else "UNRESOLVED"
        conf = "unspecified" if entity.get("_confidence_unspecified") else entity.get("confidence")
        print(f"  ? {entity.get('mention')!r} -> {entity.get('name')} [{entity.get('type')}, {action}, conf={conf}]")

    print("\nCONFLICTS")
    if not conflicts:
        print("  (none)")
    for entity, note, method in conflicts:
        matched = f" -> [[{note.title}]]" if note else ""
        print(f"  ⚠ {entity.get('mention')!r}: classified as {entity.get('type')}{matched}")

    relationships = result.get("relationships") or []
    print("\nRelationships found:")
    valid_relationships = []
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        subject = rel.get("subject") or rel.get("source")
        relation = rel.get("relation") or rel.get("type")
        obj = rel.get("object") or rel.get("target")
        if subject and relation and obj:
            valid_relationships.append((subject, relation, obj, rel.get("confidence")))
    if not valid_relationships:
        print("  (none)")
    for subject, relation, obj, confidence in valid_relationships:
        suffix = f" (conf={confidence})" if confidence is not None else ""
        print(f"  - {subject} --{relation}--> {obj}{suffix}")

    print("\nPlanned changes:")
    print("  - Add wikilinks to matched entities in target body.")
    print("  - Update target YAML entity lists.")
    if create_missing:
        print("  - Create missing entity notes from configured templates.")
    if update_reverse_links:
        print("  - Add target note to related: on linked entity notes.")
    print("  - Back up every existing file before modification.")
    print("=" * 72)


def main() -> int:
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

    min_conf = float(config.get("minimum_entity_confidence", 0.60))
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
        raw_conf = e.get("confidence")
        try:
            confidence = float(raw_conf) if raw_conf is not None else None
        except (TypeError, ValueError):
            confidence = None
        # qwen3:1.7b commonly uses 0.0 as a placeholder. Only reject an
        # explicitly positive score that is below the configured threshold.
        if confidence is not None and confidence > 0 and confidence < min_conf:
            continue
        e["type"] = etype
        e["mention"] = mention
        e["name"] = name
        e["_confidence_unspecified"] = confidence is None or confidence == 0
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

    print_plan(target, result, resolved_full, create_missing, update_reverse_links, conflict_keys)

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
        for raw in (entity.get("mention"), entity.get("name"), entity.get("existing_name")):
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


if __name__ == "__main__":
    raise SystemExit(main())
