"""Group messages into conversation threads.

Union-find over reply links (``in_reply_to`` -> ``msgid``, which crosses month
boundaries), with a fallback edge between messages that share a non-generic
subject. Returns the connected components.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

_SUBJECT_MINLEN = 8       # shorter subjects are too generic to imply a thread
_TID_LEN = 12             # hex chars of the thread id (like msg_name)


def subject_key(subject: str):
    """Normalised subject used as a thread-grouping edge, or None if too short."""
    s = (subject or "").strip().lower()
    return s if len(s) > _SUBJECT_MINLEN else None


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

    subj_first: dict[str, int] = {}      # edges from a shared subject
    for r in rows:
        k = subject_key(r["subject"])
        if k is None:
            continue
        if k in subj_first:
            union(r["id"], subj_first[k])
        else:
            subj_first[k] = r["id"]

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

def _order(r):
    return (r["date_iso"] is None, r["date_iso"] or "", r["id"])


def _tid(msgid: str) -> str:
    return hashlib.sha1((msgid or "").encode()).hexdigest()[:_TID_LEN]


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
