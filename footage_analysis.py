#!/usr/bin/env python3
"""Footage analysis helpers for Silence Cutter — The Unbound Edition.

This module intentionally stays dependency-free. It parses SRT files, builds
conversation-sized candidate moments, applies lightweight deterministic ranking,
and persists a compact sidecar index. It never loads video frames into memory.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from unbound_utils import atomic_write_json, normalize_quotes, seconds_to_clock


SPEAKER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _'’.-]{1,40})\s*:\s*(.+)$", re.S)
WORD_RE = re.compile(r"[A-Za-z0-9']+")

# Words that look like "Name:" at the start of a line but are almost never a
# speaker label ("Wait: hold on", "Note: ..."). Checked case-insensitively.
NOT_SPEAKERS = {
    "wait", "note", "notes", "okay", "ok", "look", "listen", "well", "so", "yes",
    "no", "hey", "oh", "now", "and", "but", "because", "anyway", "fine", "right",
    "also", "warning", "update", "ps", "p.s", "quote", "answer", "question", "q", "a",
    "example", "rule", "step", "reminder", "edit", "fact", "tip", "hint",
}

# A hint word that shows up in more than this share of subtitle lines is
# treated as filler and down-weighted proportionally (see _hint_weights).
COMMON_WORD_SHARE = 0.05

# These are intentionally small and transparent. They are ranking hints, not an AI
# replacement. The output remains editable by the user and is meant to surface
# likely moments for rapid review.
FUNNY_HINTS = {
    "haha", "hahaha", "laugh", "funny", "idiot", "bastard", "fuck", "shit",
    "damn", "hell", "what", "seriously", "ridiculous", "absurd", "oops", "wait",
    "excuse", "apparently", "technically", "legal", "insurance", "skyrim",
}
ARGUMENT_HINTS = {
    "no", "not", "refuse", "won't", "wouldn't", "can't", "disagree", "wrong",
    "why", "hold", "stop", "listen", "instead", "but", "however", "absolutely",
    "never", "fine", "deal", "choice", "morality", "steal", "illegal",
}
EMOTIONAL_HINTS = {
    "love", "hate", "sorry", "afraid", "fear", "miss", "remember", "promise",
    "trust", "friend", "family", "alone", "lonely", "death", "died", "hurt",
    "happy", "sad", "proud", "angry", "beautiful", "home", "oath",
}
MEMORY_HINTS = {
    "remember", "earlier", "before", "last time", "again", "still", "used to",
    "yesterday", "back then", "that time", "you said", "we said", "told you",
    "diary", "memory", "previous", "already", "once",
}
DOCUMENTARY_HINTS = {
    "remember", "choice", "decide", "argue", "think", "personality", "name",
    "unbound", "guild", "travel", "walk", "fast travel", "soul tear", "game",
    "wager", "dice", "dragonborn", "chim", "ai", "memory", "diary", "tavern",
}

MODE_LABELS = (
    "Best Overall",
    "Funniest",
    "Character-Defining",
    "Arguments / Disagreements",
    "Emotional",
    "Documentary Evidence",
    "Smash Cuts",
    "Longer Scenes",
    "Memory / Callback Moments",
)

STATUS_VALUES = ("UNREVIEWED", "KEEP", "MAYBE", "TRASH")


@dataclass
class SubtitleEntry:
    index: int
    start: float
    end: float
    text: str
    speaker: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Moment:
    id: str
    start: float
    end: float
    text: str
    speakers: list[str] = field(default_factory=list)
    score: float = 0.0
    rank: int = 0
    category: str = "Best Overall"
    reason: str = ""
    status: str = "UNREVIEWED"
    title: str = ""
    notes: str = ""
    chapter: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class SRTError(ValueError):
    pass


def _time_to_seconds(value: str) -> float:
    value = value.strip().replace(".", ",")
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}),([0-9]{1,3})", value)
    if not match:
        raise SRTError(f"Invalid SRT timestamp: {value!r}")
    h, m, s, ms = match.groups()
    ms = ms.ljust(3, "0")[:3]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt_text(text: str, known_speakers: Iterable[str] | None = None) -> list[SubtitleEntry]:
    """Parse SRT text.

    If known_speakers is given (e.g. the show's cast), only those names are
    accepted as "Name: line" speaker labels; anything else stays in the text.
    """
    known = {k.strip().lower() for k in (known_speakers or []) if k and k.strip()}
    text = normalize_quotes(text).replace("\r\n", "\n").replace("\r", "\n").strip("\ufeff\n ")
    if not text:
        raise SRTError("The SRT file is empty.")

    blocks = re.split(r"\n\s*\n", text)
    entries: list[SubtitleEntry] = []
    fallback_index = 1

    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue

        if "-->" in lines[0]:
            idx = fallback_index
            timing_line = lines[0]
            body_lines = lines[1:]
        elif len(lines) >= 3 and "-->" in lines[1]:
            try:
                idx = int(lines[0].strip())
            except ValueError:
                idx = fallback_index
            timing_line = lines[1]
            body_lines = lines[2:]
        else:
            continue

        try:
            start_raw, end_raw = [part.strip().split()[0] for part in timing_line.split("-->", 1)]
            start = _time_to_seconds(start_raw)
            end = _time_to_seconds(end_raw)
        except Exception as exc:
            raise SRTError(f"Could not parse subtitle timing line: {timing_line!r}") from exc

        if end < start:
            start, end = end, start

        body = " ".join(body_lines)
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\{\\[^}]+\}", "", body)
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            continue

        speaker = ""
        match = SPEAKER_RE.match(body)
        if match:
            candidate = match.group(1).strip()
            lowered = candidate.lower().rstrip(".")
            accepted = (lowered in known) if known else (lowered not in NOT_SPEAKERS)
            if accepted:
                speaker = candidate
                body = match.group(2).strip()

        entries.append(SubtitleEntry(idx, start, end, body, speaker))
        fallback_index += 1

    if not entries:
        raise SRTError("No subtitle entries could be parsed from this SRT file.")

    entries.sort(key=lambda e: (e.start, e.end, e.index))
    return entries


def parse_srt(path: str | Path, known_speakers: Iterable[str] | None = None) -> list[SubtitleEntry]:
    path = Path(path)
    return parse_srt_text(path.read_text(encoding="utf-8-sig", errors="replace"), known_speakers)


def infer_speakers(entries: Sequence[SubtitleEntry]) -> list[str]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.speaker:
            counts[entry.speaker] = counts.get(entry.speaker, 0) + 1
    return sorted(counts, key=lambda name: (-counts[name], name.lower()))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _words(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(normalize_quotes(text))]


def _hint_count(
    lower: str,
    counts: Counter,
    hints: Iterable[str],
    weights: dict[str, float] | None = None,
) -> tuple[int, float]:
    """Return (raw hits, weighted hits) for a hint set.

    lower is the moment text already lowercased and quote-normalized, and counts is
    a Counter of its words, so the text is tokenized once per moment rather than
    once per hint. Weights (0..1) down-rank hint words that are common filler in
    this particular transcript; phrases always count fully.
    """
    raw = 0
    weighted = 0.0
    for hint in hints:
        if " " in hint:
            n = lower.count(hint)
            w = 1.0
        else:
            n = counts.get(hint, 0)
            w = 1.0 if weights is None else weights.get(hint, 1.0)
        raw += n
        weighted += n * w
    return raw, weighted


_ALL_HINTS = (FUNNY_HINTS | ARGUMENT_HINTS | EMOTIONAL_HINTS | MEMORY_HINTS | DOCUMENTARY_HINTS)


def _hint_weights(entries: Sequence[SubtitleEntry]) -> dict[str, float]:
    """Weight each single-word hint by how rare it is in this transcript.

    A hint that appears in <= COMMON_WORD_SHARE of subtitle lines keeps weight 1.0.
    One appearing in half of all lines (e.g. "no", "but", "what") drops to ~0.1,
    so scores stop tracking plain word count.
    """
    total = max(1, len(entries))
    doc_freq: Counter = Counter()
    for entry in entries:
        doc_freq.update(set(_words(entry.text)))
    weights: dict[str, float] = {}
    for hint in _ALL_HINTS:
        if " " in hint:
            continue
        share = doc_freq.get(hint, 0) / total
        weights[hint] = 1.0 if share <= COMMON_WORD_SHARE else COMMON_WORD_SHARE / share
    return weights


def _question_count(text: str) -> int:
    return text.count("?")


def _exclaim_count(text: str) -> int:
    return text.count("!")


def _speaker_list(entries: Sequence[SubtitleEntry]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for e in entries:
        if e.speaker and e.speaker.lower() not in seen:
            out.append(e.speaker)
            seen.add(e.speaker.lower())
    return out


def _moment_text(entries: Sequence[SubtitleEntry]) -> str:
    parts = []
    for entry in entries:
        prefix = f"{entry.speaker}: " if entry.speaker else ""
        parts.append(prefix + entry.text)
    return _normalize(" ".join(parts))


def build_scene_candidates(
    entries: Sequence[SubtitleEntry],
    max_gap: float = 6.0,
    min_duration: float = 8.0,
    max_duration: float = 95.0,
) -> list[Moment]:
    """Build coherent conversational candidates without holding media in memory."""
    if not entries:
        return []

    groups: list[list[SubtitleEntry]] = []
    current: list[SubtitleEntry] = []

    for entry in entries:
        if not current:
            current = [entry]
            continue
        gap = entry.start - current[-1].end
        proposed_duration = entry.end - current[0].start
        if gap > max_gap or proposed_duration > max_duration:
            groups.append(current)
            current = [entry]
        else:
            current.append(entry)
    if current:
        groups.append(current)

    moments: list[Moment] = []
    counter = 1
    for group in groups:
        duration = group[-1].end - group[0].start
        if duration < min_duration and len(group) < 2:
            continue
        moments.append(Moment(
            id=f"scene-{counter:04d}",
            start=group[0].start,
            end=group[-1].end,
            text=_moment_text(group),
            speakers=_speaker_list(group),
        ))
        counter += 1

        # Long groups also yield overlapping sub-scenes so one excellent exchange
        # is not buried inside a 90-second candidate.
        if duration >= 40 and len(group) >= 5:
            window = 4
            for pos in range(0, len(group) - window + 1, 2):
                sub = group[pos:pos + window]
                sub_duration = sub[-1].end - sub[0].start
                if 8 <= sub_duration <= 55:
                    moments.append(Moment(
                        id=f"scene-{counter:04d}",
                        start=sub[0].start,
                        end=sub[-1].end,
                        text=_moment_text(sub),
                        speakers=_speaker_list(sub),
                    ))
                    counter += 1

    return _dedupe_moments(moments)


def build_smash_candidates(entries: Sequence[SubtitleEntry]) -> list[Moment]:
    moments: list[Moment] = []
    counter = 1
    n = len(entries)
    for i, entry in enumerate(entries):
        candidates = [[entry]]
        if i + 1 < n and entries[i + 1].start - entry.end <= 3.0:
            candidates.append([entry, entries[i + 1]])
        if i + 2 < n and entries[i + 2].start - entries[i + 1].end <= 2.5:
            candidates.append([entry, entries[i + 1], entries[i + 2]])

        for group in candidates:
            duration = group[-1].end - group[0].start
            if not (2.0 <= duration <= 18.0):
                continue
            text = _moment_text(group)
            if len(_words(text)) < 4:
                continue
            moments.append(Moment(
                id=f"smash-{counter:04d}",
                start=group[0].start,
                end=group[-1].end,
                text=text,
                speakers=_speaker_list(group),
            ))
            counter += 1
    return _dedupe_moments(moments)


def _dedupe_moments(moments: Sequence[Moment]) -> list[Moment]:
    out: list[Moment] = []
    seen: set[tuple[int, int, str]] = set()
    for moment in moments:
        key = (round(moment.start * 2), round(moment.end * 2), moment.text[:120].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(moment)
    return out


def _contains_character(moment: Moment, character: str) -> bool:
    if not character.strip():
        return True
    needle = character.strip().lower()
    if any(needle == s.lower() or needle in s.lower() for s in moment.speakers):
        return True
    return needle in moment.text.lower()


def _score_moment(moment: Moment, mode: str, weights: dict[str, float] | None = None) -> tuple[float, str]:
    text = moment.text
    lower = normalize_quotes(text).lower()
    words = _words(lower)
    counts = Counter(words)
    word_count = len(words)
    duration = max(0.1, moment.duration)
    speakers = len(moment.speakers)
    questions = _question_count(text)
    exclaims = _exclaim_count(text)
    # Raw counts feed the human-readable "why"; weighted counts feed the score.
    funny_n, funny = _hint_count(lower, counts, FUNNY_HINTS, weights)
    arguments_n, arguments = _hint_count(lower, counts, ARGUMENT_HINTS, weights)
    emotional_n, emotional = _hint_count(lower, counts, EMOTIONAL_HINTS, weights)
    memory_n, memory = _hint_count(lower, counts, MEMORY_HINTS, weights)
    documentary_n, documentary = _hint_count(lower, counts, DOCUMENTARY_HINTS, weights)
    quoted_energy = min(4, questions + exclaims)

    # Dense enough to contain content, but not rewarded indefinitely for length.
    density = min(12.0, word_count / max(4.0, duration) * 2.0)
    interaction = min(6.0, speakers * 1.75)
    baseline = min(8.0, math.log2(max(2, word_count)) * 1.6) + density + interaction + quoted_energy

    mode = mode if mode in MODE_LABELS else "Best Overall"
    reason_bits: list[str] = []

    if mode == "Funniest":
        score = baseline + funny * 5.0 + exclaims * 1.5 + questions * 0.5
        if funny >= 0.5: reason_bits.append(f"{funny_n} humor/banter signal{'s' if funny_n != 1 else ''}")
        if exclaims: reason_bits.append("high-energy delivery")
    elif mode == "Character-Defining":
        score = baseline + interaction * 1.5 + documentary * 2.0 + arguments * 1.5 + emotional * 1.5
        if moment.speakers: reason_bits.append("named character dialogue")
        if arguments >= 0.5: reason_bits.append("clear opinion or boundary")
        if emotional >= 0.5: reason_bits.append("personal/emotional language")
    elif mode == "Arguments / Disagreements":
        score = baseline + arguments * 5.0 + questions * 1.5 + interaction * 1.5
        if arguments >= 0.5: reason_bits.append(f"{arguments_n} disagreement/negation signal{'s' if arguments_n != 1 else ''}")
        if speakers >= 2: reason_bits.append("multi-speaker exchange")
    elif mode == "Emotional":
        score = baseline + emotional * 5.0 + memory * 2.0
        if emotional >= 0.5: reason_bits.append(f"{emotional_n} emotional signal{'s' if emotional_n != 1 else ''}")
        if memory >= 0.5: reason_bits.append("callback language")
    elif mode == "Documentary Evidence":
        score = baseline + documentary * 4.0 + memory * 2.5 + arguments * 1.5 + interaction
        if documentary >= 0.5: reason_bits.append("matches documentary themes")
        if memory >= 0.5: reason_bits.append("memory/callback evidence")
        if speakers >= 2: reason_bits.append("interaction between characters")
    elif mode == "Smash Cuts":
        ideal = max(0.0, 10.0 - abs(duration - 7.5) * 1.25)
        score = baseline + ideal + funny * 3.0 + arguments * 2.0 + quoted_energy * 1.5
        reason_bits.append(f"{duration:.1f}s punchy excerpt")
        if questions or exclaims: reason_bits.append("strong line ending")
    elif mode == "Longer Scenes":
        ideal = max(0.0, 14.0 - abs(duration - 50.0) * 0.25)
        score = baseline + ideal + interaction * 1.5 + documentary * 2.0
        reason_bits.append(f"{duration:.0f}s scene-length exchange")
        if speakers >= 2: reason_bits.append("conversation rather than isolated line")
    elif mode == "Memory / Callback Moments":
        score = baseline + memory * 6.0 + documentary * 1.5 + emotional
        if memory >= 0.5: reason_bits.append(f"{memory_n} memory/callback signal{'s' if memory_n != 1 else ''}")
    else:
        score = baseline + funny * 1.5 + arguments * 1.75 + emotional * 1.5 + memory * 2.0 + documentary * 2.0
        if speakers >= 2: reason_bits.append("multi-character interaction")
        if documentary >= 0.5: reason_bits.append("documentary-relevant language")
        if max(funny, arguments, emotional, memory) >= 0.5:
            reason_bits.append("strong conversational signal")

    # Very repetitive / tiny text should not float upward solely through punctuation.
    unique_ratio = len(set(words)) / max(1, word_count)
    score *= 0.65 + 0.35 * min(1.0, unique_ratio * 1.8)

    if not reason_bits:
        reason_bits.append("dense conversational exchange")
    return score, "; ".join(reason_bits[:3])


def rank_moments(
    entries: Sequence[SubtitleEntry],
    mode: str = "Best Overall",
    top_n: int = 20,
    character: str = "",
    query: str = "",
) -> list[Moment]:
    if mode == "Smash Cuts":
        candidates = build_smash_candidates(entries)
    else:
        candidates = build_scene_candidates(entries)
        # A few short moments improve every ranking mode without dominating it.
        candidates.extend(build_smash_candidates(entries))
        candidates = _dedupe_moments(candidates)

    query_terms = _words(query)
    weights = _hint_weights(entries)
    ranked: list[Moment] = []

    for moment in candidates:
        if not _contains_character(moment, character):
            continue
        lower = normalize_quotes(moment.text).lower()
        if query_terms and not all(term in lower for term in query_terms):
            continue
        score, reason = _score_moment(moment, mode, weights)
        moment.score = round(score, 3)
        moment.category = mode
        moment.reason = reason
        ranked.append(moment)

    ranked.sort(key=lambda m: (-m.score, m.start, m.end))

    # Diversity filter: avoid returning a dozen heavily overlapping variants of
    # the same conversation. A high-scoring candidate wins its neighborhood.
    chosen: list[Moment] = []
    for moment in ranked:
        overlap = False
        for other in chosen:
            intersection = max(0.0, min(moment.end, other.end) - max(moment.start, other.start))
            shorter = max(0.1, min(moment.duration, other.duration))
            if intersection / shorter >= 0.72:
                overlap = True
                break
        if not overlap:
            chosen.append(moment)
        if len(chosen) >= max(1, int(top_n)):
            break

    chosen.sort(key=lambda m: -m.score)
    for idx, moment in enumerate(chosen, 1):
        moment.rank = idx
        # ID depends only on the time span, so the same clip keeps the same ID
        # across re-analysis (sequence items link back to it via source_moment_id).
        moment.id = moment_id_for(moment.start, moment.end)
        if not moment.title:
            snippet = re.sub(r"\s+", " ", moment.text).strip()
            moment.title = snippet[:82] + ("…" if len(snippet) > 82 else "")
    return chosen


def moment_id_for(start: float, end: float) -> str:
    return f"moment-{int(round(start * 1000)):010d}-{int(round(end * 1000)):010d}"


def transcript_lines(entries: Sequence[SubtitleEntry]) -> list[str]:
    lines = []
    for entry in entries:
        speaker = f"{entry.speaker}: " if entry.speaker else ""
        lines.append(f"{seconds_to_clock(entry.start)}–{seconds_to_clock(entry.end)}  {speaker}{entry.text}")
    return lines


def search_entries(entries: Sequence[SubtitleEntry], query: str) -> list[SubtitleEntry]:
    terms = _words(query)
    if not terms:
        return list(entries)
    out = []
    for entry in entries:
        haystack = normalize_quotes(f"{entry.speaker} {entry.text}").lower()
        if all(term in haystack for term in terms):
            out.append(entry)
    return out


def sidecar_path_for(video_path: str | Path) -> Path:
    path = Path(video_path)
    return path.with_name(path.stem + "_footage_index.json")


def make_index_payload(
    video_path: str | Path,
    srt_path: str | Path,
    entries: Sequence[SubtitleEntry],
    moments: Sequence[Moment],
    video_duration: float | None = None,
    mode: str = "Best Overall",
    character_filter: str = "",
    query: str = "",
) -> dict:
    return {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_video": str(Path(video_path)),
        "source_srt": str(Path(srt_path)),
        "video_duration_seconds": video_duration,
        "subtitle_count": len(entries),
        "subtitle_end_seconds": max((e.end for e in entries), default=0.0),
        "speakers": infer_speakers(entries),
        "analysis": {
            "mode": mode,
            "character_filter": character_filter,
            "query": query,
            "moment_count": len(moments),
        },
        "moments": [asdict(m) | {"duration": m.duration} for m in moments],
    }


def save_index(path: str | Path, payload: dict) -> Path:
    """Atomic save: an interrupted write never corrupts hand-triaged review data."""
    return atomic_write_json(path, payload)


def load_index(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "moments" not in data:
        raise ValueError("This file is not a Silence Slicer footage index.")
    return data


def moments_from_index(payload: dict, skipped: list[str] | None = None) -> list[Moment]:
    """Load moments from an index payload.

    Entries that can't be loaded are skipped; if a `skipped` list is passed, a short
    reason for each is appended to it so the caller can tell the user.
    """
    out = []
    allowed = set(Moment.__dataclass_fields__)
    for n, raw in enumerate(payload.get("moments", []), 1):
        if not isinstance(raw, dict):
            if skipped is not None:
                skipped.append(f"moment #{n}: not an object")
            continue
        cleaned = {k: v for k, v in raw.items() if k in allowed}
        try:
            out.append(Moment(**cleaned))
        except TypeError as exc:
            if skipped is not None:
                skipped.append(f"moment #{n}: {exc}")
    return out


def export_moments_csv(path: str | Path, moments: Sequence[Moment], source_video: str = "") -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "rank", "start", "end", "duration_seconds", "status", "category",
            "title", "characters", "reason", "chapter", "tags", "notes",
            "source_video", "transcript_excerpt",
        ])
        for m in moments:
            writer.writerow([
                m.rank,
                seconds_to_clock(m.start, millis=True),
                seconds_to_clock(m.end, millis=True),
                f"{m.duration:.3f}",
                m.status,
                m.category,
                m.title,
                "; ".join(m.speakers),
                m.reason,
                m.chapter,
                "; ".join(m.tags),
                m.notes,
                source_video,
                m.text,
            ])
    return path


def export_moments_txt(path: str | Path, moments: Sequence[Moment], source_video: str = "") -> Path:
    path = Path(path)
    lines = []
    if source_video:
        lines.extend([f"Source: {source_video}", ""])
    for m in moments:
        chars = ", ".join(m.speakers) if m.speakers else "—"
        lines.extend([
            f"#{m.rank:02d}  {seconds_to_clock(m.start)} - {seconds_to_clock(m.end)}  [{m.status}]",
            f"{m.title}",
            f"Characters: {chars}",
            f"Category: {m.category}",
            f"Why: {m.reason}",
            f"Chapter: {m.chapter or '—'}",
            f"Notes: {m.notes or '—'}",
            f"Transcript: {m.text}",
            "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def export_moments_json(path: str | Path, payload: dict) -> Path:
    return save_index(path, payload)
