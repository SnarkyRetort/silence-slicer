#!/usr/bin/env python3
"""Story cross-reference helpers for Silence Slicer — The Unbound Edition.

Dependency-free matching between diary-roll CSV entries and footage-analysis
moments. The matcher is deliberately transparent: it scores rare shared terms,
character/name overlap, callback vocabulary, and quoted phrase overlap.
"""
from __future__ import annotations

import csv
import json
import math
import re
from difflib import SequenceMatcher
from datetime import datetime
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from unbound_utils import atomic_write_json

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.-]{2,}")
STOP = {
    "the","and","that","this","with","from","have","had","was","were","are","but","for","not","you","your","our","they","their","them","his","her","she","him","its","into","out","about","just","then","than","when","what","who","why","how","there","here","still","been","being","would","could","should","will","can","cant","won't","dont","didnt","did","does","after","before","over","under","more","some","one","two","all","any","only","very","now","like","well","even","back","again","down","up","off","too","got","get","getting","make","made","say","said","says","thing","things","time","day","night","good","really"
}
CALLBACK = {"remember","earlier","before","again","still","told","said","promised","promise","last","yesterday","once","diary","journal","note","wager","bet","owed","name","called"}


def parse_clock(value: str) -> float:
    value = str(value or "").strip()
    if not value:
        return 0.0
    parts = value.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h)*3600 + int(m)*60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m)*60 + float(s)
        return float(value)
    except Exception:
        return 0.0


def clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    h = int(seconds // 3600); m = int((seconds % 3600)//60); s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


_POSSESSIVE_RE = re.compile(r"'s$")


def _tokens(text: str, keep_stop: bool = False) -> list[str]:
    """Lowercase content words with surrounding punctuation stripped.

    "Whiterun." and "Whiterun" both become "whiterun", and a possessive
    "Lydia's" becomes "lydia", so names and places match however they're
    punctuated. keep_stop=True keeps stopwords (used for callback detection).
    """
    out = []
    for raw in WORD_RE.findall(str(text or "")):
        w = raw.lower().replace("\u2019", "'").strip(".-'")
        w = _POSSESSIVE_RE.sub("", w)
        if len(w) < 3:
            continue
        if not keep_stop and w in STOP:
            continue
        out.append(w)
    return out


def _title_from(row: dict) -> str:
    return (row.get("title") or row.get("transcript_excerpt") or row.get("text") or "Diary entry").strip()

@dataclass
class DiaryEntry:
    id: str
    start: float
    end: float
    title: str
    text: str
    characters: str = ""
    source_video: str = ""
    source_csv: str = ""
    status: str = ""
    category: str = ""
    tags: str = ""

@dataclass
class FootageMoment:
    id: str
    start: float
    end: float
    title: str
    text: str
    characters: str = ""
    source_video: str = ""
    source_index: str = ""
    status: str = ""
    category: str = ""
    tags: str = ""

@dataclass
class CrossReference:
    diary_id: str
    moment_id: str
    score: float
    reason: str
    diary_title: str
    diary_start: float
    diary_end: float
    diary_video: str
    moment_title: str
    moment_start: float
    moment_end: float
    moment_video: str
    shared_terms: list[str]
    relation_type: str = "THEMATIC LINK"


def _norm_text(text: str) -> str:
    """Normalize text for duplicate/same-passage detection."""
    return " ".join(_tokens(text))


def _text_similarity(a: str, b: str) -> float:
    """Blend token Jaccard and sequence similarity for noisy transcripts."""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    sa, sb = set(na.split()), set(nb.split())
    jacc = len(sa & sb) / max(1, len(sa | sb))
    seq = SequenceMatcher(None, na, nb, autojunk=False).ratio()
    return max(jacc, seq * 0.92)


def _interval_overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    if a1 < a0: a0, a1 = a1, a0
    if b1 < b0: b0, b1 = b1, b0
    overlap = max(0.0, min(a1,b1)-max(a0,b0))
    base = max(0.001, min(max(0.001,a1-a0), max(0.001,b1-b0)))
    return overlap/base


def _source_family(path: str) -> str:
    """Collapse obvious duplicate render suffixes such as _005."""
    if not path:
        return ""
    p=Path(path)
    stem=p.stem.lower()
    stem=re.sub(r"_(?:copy|duplicate|dup|v\d+|\d{3,})$", "", stem)
    return str(p.with_name(stem+p.suffix.lower())).lower()


def _source_datetime(path: str):
    """Parse common OBS-style YYYY-MM-DD_HH-MM-SS names when available."""
    name=Path(path or "").name
    m=re.search(r"(20\d{2})-(\d{2})-(\d{2})[_ -](\d{2})[-:](\d{2})[-:](\d{2})", name)
    if not m:
        return None
    try:
        return datetime(*map(int,m.groups()))
    except Exception:
        return None


def _is_self_or_duplicate(d: DiaryEntry, m: FootageMoment) -> bool:
    """Reject identity matches and duplicate renders of the same passage."""
    sim=_text_similarity(d.text, m.text)
    same_path=bool(d.source_video and m.source_video and Path(d.source_video)==Path(m.source_video))
    same_family=bool(d.source_video and m.source_video and _source_family(d.source_video)==_source_family(m.source_video))
    overlap=_interval_overlap_ratio(d.start,d.end,m.start,m.end)
    near_time=abs(d.start-m.start) <= 2.0 and abs(d.end-m.end) <= 3.0
    title_sim=_text_similarity(d.title,m.title)
    # Same file + overlapping timestamp is the same source passage, not a story link.
    if same_path and overlap >= 0.25:
        return True
    # Duplicate render/copy with near-identical timing and text.
    if same_family and (near_time or overlap >= 0.65) and sim >= 0.72:
        return True
    # Repeated near-verbatim passage inside the same source/render family is a
    # duplicate recital, not an independent story callback.
    if same_family and sim >= 0.88:
        return True
    # Exact/near-exact text and timing even when paths differ unexpectedly.
    if near_time and (sim >= 0.88 or title_sim >= 0.92):
        return True
    return False


def _relation_type(d: DiaryEntry, m: FootageMoment, sim: float, char_overlap: set[str], callback: set[str]) -> str:
    dd, md = _source_datetime(d.source_video), _source_datetime(m.source_video)
    if dd and md and md < dd:
        return "SOURCE EVENT"
    if dd and md and md > dd and callback:
        return "LATER CALLBACK"
    if sim >= 0.58:
        if d.characters and m.characters and not char_overlap:
            return "SAME EVENT / DIFFERENT CHARACTER"
        return "DIARY RETELLING"
    if callback:
        return "CALLBACK / MEMORY LINK"
    if char_overlap and sim >= 0.30:
        return "SAME EVENT / CHARACTER LINK"
    return "THEMATIC LINK"


def load_diary_csv(path: str | Path) -> list[DiaryEntry]:
    path = Path(path)
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 1):
            text = (row.get("transcript_excerpt") or row.get("text") or row.get("reason") or "").strip()
            if not text:
                continue
            start = parse_clock(row.get("start", "")); end = parse_clock(row.get("end", ""))
            if end < start: start, end = end, start
            rows.append(DiaryEntry(
                id=f"diary-{n}-{int(start*1000)}",
                start=start, end=end,
                title=_title_from(row), text=text,
                characters=(row.get("characters") or "").strip(),
                source_video=(row.get("source_video") or "").strip(),
                source_csv=str(path),
                status=(row.get("status") or "").strip(),
                category=(row.get("category") or "").strip(),
                tags=(row.get("tags") or "").strip(),
            ))
    return rows


def load_footage_index(path: str | Path) -> list[FootageMoment]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    source_video = str(data.get("source_video") or data.get("video") or "")
    out = []
    for n, m in enumerate(data.get("moments", []) or [], 1):
        text = str(m.get("text") or m.get("transcript_excerpt") or "").strip()
        if not text: continue
        speakers = m.get("speakers") or m.get("characters") or []
        if isinstance(speakers, list): speakers = ", ".join(str(x) for x in speakers)
        tags = m.get("tags") or []
        if isinstance(tags, list): tags = ", ".join(str(x) for x in tags)
        out.append(FootageMoment(
            id=str(m.get("id") or f"moment-{n}"),
            start=float(m.get("start", 0) or 0), end=float(m.get("end", 0) or 0),
            title=str(m.get("title") or text[:90]), text=text,
            characters=str(speakers), source_video=source_video, source_index=str(path),
            status=str(m.get("status") or ""), category=str(m.get("category") or ""), tags=str(tags),
        ))
    return out


def moments_from_csv(path: str | Path) -> list[FootageMoment]:
    path = Path(path); out=[]
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for n,row in enumerate(csv.DictReader(f),1):
            text=(row.get("transcript_excerpt") or row.get("text") or "").strip()
            if not text: continue
            out.append(FootageMoment(
                id=f"csvmoment-{n}", start=parse_clock(row.get("start","")), end=parse_clock(row.get("end","")),
                title=_title_from(row), text=text, characters=(row.get("characters") or "").strip(),
                source_video=(row.get("source_video") or "").strip(), source_index=str(path),
                status=(row.get("status") or "").strip(), category=(row.get("category") or "").strip(), tags=(row.get("tags") or "").strip(),
            ))
    return out


def _idf(docs: list[list[str]]) -> dict[str,float]:
    n=max(1,len(docs)); df=Counter()
    for d in docs: df.update(set(d))
    return {w: math.log((n+1)/(c+1))+1.0 for w,c in df.items()}


def build_crossrefs(diaries: Iterable[DiaryEntry], moments: Iterable[FootageMoment], *, min_score: float=0.18, max_per_diary: int=5, query: str="") -> list[CrossReference]:
    diaries=list(diaries); moments=list(moments)
    qtokens=set(_tokens(query)) if query else set()
    docs=[_tokens(d.text+" "+d.title+" "+d.characters) for d in diaries] + [_tokens(m.text+" "+m.title+" "+m.characters) for m in moments]
    idf=_idf(docs)
    mtoks=[_tokens(m.text+" "+m.title+" "+m.characters+" "+m.tags) for m in moments]
    # Callback words like "before"/"again"/"said" are also stopwords, so detect
    # them on the unfiltered text; otherwise they could never match.
    m_callbacks=[set(_tokens(m.text+" "+m.title, keep_stop=True)) & CALLBACK for m in moments]
    out=[]
    for d in diaries:
        dt=_tokens(d.text+" "+d.title+" "+d.characters+" "+d.tags); ds=set(dt)
        d_callbacks=set(_tokens(d.text+" "+d.title, keep_stop=True)) & CALLBACK
        if qtokens and not qtokens.intersection(ds):
            continue
        scored=[]
        dchars=set(_tokens(d.characters))
        for m,toks,mcb in zip(moments,mtoks,m_callbacks):
            if _is_self_or_duplicate(d, m):
                continue
            ms=set(toks); shared=ds & ms
            if not shared: continue
            sim=_text_similarity(d.text, m.text)
            weighted=sum(idf.get(w,1.0) for w in shared)
            denom=max(1.0, math.sqrt(sum(idf.get(w,1.0) for w in ds))*math.sqrt(sum(idf.get(w,1.0) for w in ms)))
            lexical=weighted/denom
            mchars=set(_tokens(m.characters)); char_overlap=dchars & mchars
            callback=d_callbacks & mcb
            # Boost distinctive proper-looking/shared event terms and callbacks.
            score=lexical + min(0.22, len(char_overlap)*0.07) + min(0.18, len(callback)*0.05)
            # Exact multiword phrase fragment (4+ words) is strong evidence.
            mn=" ".join(toks)
            phrase=False
            for i in range(max(0,len(dt)-3)):
                p=" ".join(dt[i:i+4])
                if p and p in mn:
                    phrase=True; break
            if phrase: score += 0.18
            # Very high verbatim overlap is usually a retelling/duplicate candidate;
            # keep true retellings, but prevent them from dominating every ranking.
            if sim >= 0.72:
                score *= 0.82
            if score < min_score: continue
            top=sorted(shared,key=lambda w:idf.get(w,1.0),reverse=True)[:8]
            reasons=[]
            if top: reasons.append("shared: "+", ".join(top[:5]))
            if char_overlap: reasons.append("same character(s): "+", ".join(sorted(char_overlap)))
            if callback: reasons.append("callback language")
            if phrase: reasons.append("matching phrase")
            relation=_relation_type(d,m,sim,char_overlap,callback)
            reasons.insert(0, relation)
            scored.append((score,m,top,"; ".join(reasons),relation))
        # De-duplicate equivalent moment candidates before limiting per diary.
        scored.sort(key=lambda x:x[0], reverse=True)
        unique=[]; seen=set()
        for item in scored:
            score,m,top,reason,relation=item
            sig=(_source_family(m.source_video), round(m.start,1), round(m.end,1), _norm_text(m.text)[:220])
            if sig in seen:
                continue
            seen.add(sig); unique.append(item)
        for score,m,top,reason,relation in unique[:max_per_diary]:
            out.append(CrossReference(
                diary_id=d.id, moment_id=m.id, score=round(score,4), reason=reason,
                diary_title=d.title, diary_start=d.start, diary_end=d.end, diary_video=d.source_video,
                moment_title=m.title, moment_start=m.start, moment_end=m.end, moment_video=m.source_video,
                shared_terms=top, relation_type=relation,
            ))
    out.sort(key=lambda x:x.score, reverse=True)
    return out


def save_crossrefs_json(path: str | Path, refs: Iterable[CrossReference], diary_sources: list[str], footage_sources: list[str]) -> Path:
    path=Path(path)
    payload={"schema_version":2,"diary_sources":diary_sources,"footage_sources":footage_sources,"crossrefs":[asdict(r) for r in refs]}
    atomic_write_json(path,payload); return path


def save_crossrefs_csv(path: str | Path, refs: Iterable[CrossReference]) -> Path:
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fields=["score","relation_type","reason","diary_start","diary_end","diary_title","diary_video","moment_start","moment_end","moment_title","moment_video","shared_terms","diary_id","moment_id"]
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in refs:
            w.writerow({"score":r.score,"relation_type":r.relation_type,"reason":r.reason,"diary_start":clock(r.diary_start),"diary_end":clock(r.diary_end),"diary_title":r.diary_title,"diary_video":r.diary_video,"moment_start":clock(r.moment_start),"moment_end":clock(r.moment_end),"moment_title":r.moment_title,"moment_video":r.moment_video,"shared_terms":", ".join(r.shared_terms),"diary_id":r.diary_id,"moment_id":r.moment_id})
    return path
