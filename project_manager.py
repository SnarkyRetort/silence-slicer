#!/usr/bin/env python3
"""Project workspace support for Silence Cutter — The Unbound Edition.

A project owns one source recording and every derived artifact. Large source media
may be linked in-place; all generated outputs are routed into predictable folders
under the project root.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from unbound_utils import atomic_write_json

SCHEMA_VERSION = 3
PROJECT_FILE = "project.json"
FOLDERS = (
    "source", "processed", "transcript", "analysis", "sequences",
    "exports", "exports/clips", "narration",
)

class ProjectError(RuntimeError):
    pass

def safe_slug(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(text or "Project")).strip()
    text = re.sub(r"\s+", "_", text)
    return text or "Project"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _atomic_json(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)

def _norm_label(label: str) -> str:
    label = safe_slug(label or "processed").upper()
    return label or "PROCESSED"

@dataclass
class ProjectWorkspace:
    root: Path
    data: dict

    @property
    def manifest_path(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.root.name)

    def folder(self, key: str) -> Path:
        key = key.replace("\\", "/").strip("/")
        if key not in set(FOLDERS):
            raise ProjectError(f"Unknown project folder: {key}")
        p = self.root / Path(key)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save(self) -> Path:
        self.data["schema_version"] = SCHEMA_VERSION
        self.data["updated_utc"] = _now()
        _atomic_json(self.manifest_path, self.data)
        return self.manifest_path

    def absolute_path(self, key: str) -> Path | None:
        raw = self.data.get("assets", {}).get(key)
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    def _store_path(self, path: str | Path, prefer_relative=True) -> str:
        p = Path(path).expanduser().resolve()
        if prefer_relative:
            try:
                return p.relative_to(self.root.resolve()).as_posix()
            except ValueError:
                pass
        return str(p)

    def set_asset(self, key: str, path: str | Path | None, prefer_relative: bool = True) -> None:
        assets = self.data.setdefault("assets", {})
        assets[key] = "" if path is None else self._store_path(path, prefer_relative)
        self.save()

    def attach_source(self, source: str | Path, copy_into_project: bool = False) -> Path:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise ProjectError("Source recording does not exist.")
        if copy_into_project:
            # Canonical archive name keeps the project identity even if the original was messy.
            ext = source.suffix.lower() or ".mkv"
            dest = self.folder("source") / f"{safe_slug(self.name)}_RAW{ext}"
            if dest.exists() and dest.resolve() != source:
                dest.unlink()
            if dest.resolve() != source:
                shutil.copy2(source, dest)
            actual = dest.resolve()
            self.data["source_mode"] = "copied"
            self.set_asset("source", actual)
        else:
            actual = source
            self.data["source_mode"] = "linked"
            self.set_asset("source", actual, prefer_relative=False)
        return actual

    # ---------- processed variants ----------
    def default_processed_path(self, source: str | Path | None = None, preset: str = "High", suffix: str = ".mp4") -> Path:
        suffix = suffix if suffix.startswith(".") else "." + suffix
        label = _norm_label(preset)
        return self.folder("processed") / f"{safe_slug(self.name)}_{label}{suffix.lower()}"

    def register_processed(self, preset: str, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        label = _norm_label(preset)
        variants = self.data.setdefault("processed_variants", {})
        variants[label] = self._store_path(p, True)
        self.data["active_processed"] = label
        self.data.setdefault("assets", {})["processed"] = self._store_path(p, True)
        self.save()
        return p

    def processed_variants(self) -> dict[str, Path]:
        out = {}
        for label, raw in (self.data.get("processed_variants") or {}).items():
            if not raw: continue
            p = Path(raw)
            out[str(label)] = p if p.is_absolute() else self.root / p
        legacy = self.absolute_path("processed")
        if legacy and not out:
            out["PROCESSED"] = legacy
        return out

    def processed_for(self, label: str) -> Path | None:
        return self.processed_variants().get(_norm_label(label))

    # ---------- transcript variants ----------
    def import_transcript(self, srt: str | Path, variant: str | None = None, copy_into_project: bool = True) -> Path:
        src = Path(srt).expanduser().resolve()
        if not src.is_file():
            raise ProjectError("SRT file does not exist.")
        if src.suffix.lower() != ".srt":
            raise ProjectError("Choose an .srt subtitle file.")
        label = _norm_label(variant or self.data.get("active_processed") or "SOURCE")
        if copy_into_project:
            dest = self.folder("transcript") / f"{safe_slug(self.name)}_{label}.srt"
            if dest.resolve() != src:
                shutil.copy2(src, dest)
            actual = dest.resolve()
        else:
            actual = src
        variants = self.data.setdefault("srt_variants", {})
        variants[label] = self._store_path(actual, copy_into_project)
        self.data.setdefault("assets", {})["srt"] = self._store_path(actual, copy_into_project)
        self.save()
        return actual

    def srt_for(self, label: str) -> Path | None:
        """The SRT registered for exactly this version, or None.

        No fallback to another version's transcript: a MEDIUM video must never be
        paired with a CONVERSATION SRT. Older projects are safe because opening
        them migrates the legacy single SRT into srt_variants.
        """
        raw = (self.data.get("srt_variants") or {}).get(_norm_label(label))
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else self.root / p

    # ---------- deterministic project-owned paths ----------
    def footage_index_path(self, video: str | Path | None = None) -> Path:
        video_path = Path(video) if video else self.absolute_path("processed")
        stem = safe_slug(video_path.stem if video_path else self.name)
        return self.folder("analysis") / f"{stem}_footage_index.json"

    def analysis_path(self, name: str, suffix: str) -> Path:
        suffix = suffix if suffix.startswith(".") else "." + suffix
        return self.folder("analysis") / f"{safe_slug(name)}{suffix}"

    def sequence_path(self, name: str) -> Path:
        return self.folder("sequences") / f"{safe_slug(name)}_sequence.json"

    def export_path(self, name: str, suffix: str) -> Path:
        suffix = suffix if suffix.startswith(".") else "." + suffix
        return self.folder("exports") / f"{safe_slug(name)}{suffix}"

    def narration_path(self, name: str, suffix: str) -> Path:
        suffix = suffix if suffix.startswith(".") else "." + suffix
        return self.folder("narration") / f"{safe_slug(name)}{suffix}"

    def summary(self) -> dict:
        variants = self.processed_variants()
        active = str(self.data.get("active_processed") or "")
        active_path = variants.get(active) or self.absolute_path("processed")
        return {
            "name": self.name,
            "root": str(self.root),
            "source_mode": self.data.get("source_mode", ""),
            "source": str(self.absolute_path("source") or ""),
            "processed": str(active_path or ""),
            "processed_variants": {k: str(v) for k,v in variants.items()},
            "active_processed": active,
            "srt": str(self.absolute_path("srt") or ""),
            "footage_index": str(self.absolute_path("footage_index") or ""),
            "sequence": str(self.absolute_path("sequence") or ""),
        }


def _remap_paths_for_new_root(data: dict, old_root: Path, new_root: Path) -> dict:
    """Rewrite absolute asset paths that pointed inside old_root after a safe folder move."""
    old_root = old_root.resolve()
    new_root = new_root.resolve()

    def remap(value):
        if isinstance(value, str) and value:
            p = Path(value)
            if p.is_absolute():
                try:
                    rel = p.resolve().relative_to(old_root)
                except (ValueError, OSError):
                    return value
                return str(new_root / rel)
            return value
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remap(v) for v in value]
        return value

    return remap(data)


def flatten_duplicate_project_root(workspace: ProjectWorkspace) -> ProjectWorkspace:
    """Flatten accidental ProjectName/ProjectName nesting when it is safe to do so.

    Only runs when the current project root and its parent have the same folder name,
    the parent does not already contain a project.json, and no destination collisions
    would occur. Linked source media outside the project is left untouched.
    """
    inner = workspace.root.resolve()
    parent = inner.parent.resolve()
    if inner.name.casefold() != parent.name.casefold():
        return workspace
    if (parent / PROJECT_FILE).exists():
        return workspace

    children = list(inner.iterdir())
    collisions = [child.name for child in children if (parent / child.name).exists()]
    if collisions:
        raise ProjectError(
            "This project is double-nested, but it could not be flattened safely because "
            "the outer folder already contains: " + ", ".join(collisions)
        )

    # Rewrite any absolute in-project paths before moving the files.
    data = _remap_paths_for_new_root(dict(workspace.data), inner, parent)

    for child in children:
        shutil.move(str(child), str(parent / child.name))
    try:
        inner.rmdir()
    except OSError:
        pass

    flattened = ProjectWorkspace(parent, data)
    flattened.save()
    return flattened

def create_project(root: str | Path, name: str | None = None) -> ProjectWorkspace:
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for folder in FOLDERS:
        (root / Path(folder)).mkdir(parents=True, exist_ok=True)
    manifest = root / PROJECT_FILE
    if manifest.exists():
        raise ProjectError(f"A project already exists here: {manifest}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": name or root.name,
        "created_utc": _now(),
        "updated_utc": _now(),
        "source_mode": "",
        "active_processed": "",
        "processed_variants": {},
        "srt_variants": {},
        "assets": {"source":"", "processed":"", "srt":"", "footage_index":"", "sequence":""},
    }
    ws = ProjectWorkspace(root, payload); ws.save(); return ws

def open_project(path: str | Path) -> ProjectWorkspace:
    path = Path(path).expanduser().resolve()
    manifest = path / PROJECT_FILE if path.is_dir() else path
    if not manifest.is_file():
        raise ProjectError("Choose a Silence Slicer project folder or project.json.")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProjectError("Unsupported or invalid Silence Slicer project file.")
    # In-place migration from the original project schema.
    old = int(data.get("schema_version", 1) or 1)
    if old > SCHEMA_VERSION:
        raise ProjectError("This project was created by a newer Silence Slicer version.")
    data.setdefault("assets", {"source":"", "processed":"", "srt":"", "footage_index":"", "sequence":""})
    data.setdefault("processed_variants", {})
    data.setdefault("srt_variants", {})
    data.setdefault("active_processed", "")
    if old < 2:
        legacy = data.get("assets", {}).get("processed")
        if legacy and not data["processed_variants"]:
            data["processed_variants"]["PROCESSED"] = legacy
            data["active_processed"] = "PROCESSED"
        legacy_srt = data.get("assets", {}).get("srt")
        if legacy_srt and not data["srt_variants"]:
            data["srt_variants"][data.get("active_processed") or "PROCESSED"] = legacy_srt
    data["schema_version"] = SCHEMA_VERSION
    root = manifest.parent
    for folder in FOLDERS:
        (root / Path(folder)).mkdir(parents=True, exist_ok=True)
    ws = ProjectWorkspace(root, data)
    ws.save()
    return flatten_duplicate_project_root(ws)
