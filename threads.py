"""Group messages into conversation threads.

Union-find over reply links (``in_reply_to`` -> ``msgid``, which crosses month
boundaries), with a fallback edge between messages that share a non-generic
subject. Returns the connected components.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date as _date

_SUBJECT_MINLEN = 8       # shorter subjects are too generic to imply a thread
_TID_LEN = 12             # hex chars of the thread id (like msg_name)
_SUBJECT_WINDOW_DAYS = 90  # a subject-only edge chains messages at most this far
                           # apart; the SAME subject reused months/years later is
                           # a NEW conversation, not a continuation of the old one


def subject_key(subject: str):
    """Normalised subject used as a thread-grouping edge, or None if too short."""
    s = (subject or "").strip().lower()
    return s if len(s) > _SUBJECT_MINLEN else None


def _row_day(r):
    """The message's calendar day from the leading YYYY-MM-DD of date_iso, or
    None when absent/undated/malformed. Day granularity is enough to bound the
    subject window and dodges timezone-suffix parsing differences."""
    try:
        iso = r["date_iso"]
    except (KeyError, IndexError):
        return None                       # caller passed rows without the column
    if not iso:
        return None
    try:
        return _date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return None


def components(rows):
    """``rows``: sequence of mappings with keys id, msgid, in_reply_to, subject.
    Returns ``{root_id: [rows]}`` (single-message threads included)."""
    by_msgid = {r["msgid"]: r["id"] for r in rows if r["msgid"]}
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for r in rows:                       # edges from reply links
        irt = r["in_reply_to"]
        if irt and irt in by_msgid:
            union(r["id"], by_msgid[irt])

    # edges from a shared subject, time-bounded. Walk messages oldest-first and
    # chain each to the MOST RECENT same-subject message, but only when they are
    # within _SUBJECT_WINDOW_DAYS. A genuinely slow thread (each reply < window
    # after the last) stays one component however long its total span; two
    # messages that merely reuse a subject years apart do not merge. Undated
    # messages carry no datable edge (reply links still apply).
    day_of = {r["id"]: _row_day(r) for r in rows}
    subj_prev: dict[str, int] = {}       # subject_key -> id of newest dated msg
    for r in sorted(rows, key=lambda x: (day_of[x["id"]] is None,
                                         day_of[x["id"]] or _date.min,
                                         x["id"])):
        k = subject_key(r["subject"])
        if k is None:
            continue
        d = day_of[r["id"]]
        if d is None:
            continue
        prev = subj_prev.get(k)
        if prev is not None and (d - day_of[prev]).days <= _SUBJECT_WINDOW_DAYS:
            union(r["id"], prev)
        subj_prev[k] = r["id"]

    comp: dict[int, list] = defaultdict(list)
    for r in rows:
        comp[find(r["id"])].append(r)
    return comp


# --- stable thread ids -------------------------------------------------------
#
# A thread's id must survive rebuilds: a new reply, or a subject-merge joining
# two threads, must NOT renumber an existing thread (else every permalink to
# thread/<id> breaks). We freeze ids with a persisted msgid->tid map: a
# component keeps an id already assigned to any of its members; only a brand-new
# thread gets a fresh id, derived from its anchor (earliest message's Msg-Id).

def order(r):
    """THE chronological sort key: shared by the renderer's display roots
    (generate._sortkey) and the id-anchor choice below, so the message a
    reader sees first and the message that mints the thread id can never
    disagree."""
    return (r["date_iso"] is None, r["date_iso"] or "", r["id"])


_order = order                  # historical internal alias


def stable_id(msgid: str, n: int) -> str:
    """First ``n`` hex chars of sha1(Message-Id) -- the permanent identity
    behind thread/<tid> (n=12) and msg/<id> (n=16) permalinks. utf-8 with
    "replace" so an exotic msgid yields a stable id instead of crashing the
    rebuild (ids of encodable msgids -- i.e. all existing ones -- are
    unchanged by the replace policy)."""
    return hashlib.sha1(
        (msgid or "").encode("utf-8", "replace")).hexdigest()[:n]


def _tid(msgid: str) -> str:
    return stable_id(msgid, _TID_LEN)


def thread_ids(rows, prior=None) -> dict:
    """Return ``{msgid: thread_id}`` for all rows. ``rows`` need keys id, msgid,
    in_reply_to, subject, date_iso. ``prior`` (a previous msgid->tid map) freezes
    ids across rebuilds:
      * if the thread's anchor already had an id, keep it;
      * else if any member had an id (two old threads merged), keep the dominant;
      * else mint a fresh id from the anchor's Message-Id (stable & deterministic).
    """
    prior = prior or {}
    out: dict = {}
    for members in components(rows).values():
        anchor = min(members, key=_order)
        priors = [prior[r["msgid"]] for r in members
                  if r["msgid"] and r["msgid"] in prior]
        if anchor["msgid"] in prior:
            tid = prior[anchor["msgid"]]
        elif priors:
            tid = Counter(priors).most_common(1)[0][0]
        else:
            tid = _tid(anchor["msgid"])
        for r in members:
            if r["msgid"]:
                out[r["msgid"]] = tid
    return out


def load_map(path) -> dict:
    """Load a persisted msgid->tid map (empty if absent)."""
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return {}


def save_map(path, mapping: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, sort_keys=True)
