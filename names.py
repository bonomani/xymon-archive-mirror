"""Display-name normalisation.

Only the COSMETIC rules applied when rendering a sender's name. The resolution
of *which* name to show (override / backfill / derive-from-address) lives in
obfuscate.py and is baked into the DB; this module just tidies the result.
"""
from __future__ import annotations

# nobiliary particles kept lowercase when not the first word
_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da",
              "du", "la", "le", "ten", "ter", "vom", "zu", "of", "op"}
_INITIALS_MAXLEN = 4      # 'J.C.' is initials; a longer dotted token is left as-is
_CASEFIX_MINLEN = 3       # only title-case all-caps/all-lower words this long


def clean(n: str) -> str:
    """'Last, First' -> 'First Last' (single comma); ALL-CAPS or all-lowercase
    words title-cased ('Cédric BRINER', 'deepak deore' -> proper case; mixed-case
    like 'McConnell' kept); dotted initials upper-cased; particles kept lower."""
    if not n:
        return n
    if n.count(",") == 1 and "@" not in n:
        last, first = (p.strip() for p in n.split(",", 1))
        if last and first:
            n = f"{first} {last}"

    def _fix(w: str, first: bool) -> str:
        if "." in w:                      # initials: 'j.c.'/'J.C.' -> 'J.C.'
            base = w.replace(".", "")
            return w.upper() if (base.isalpha()
                                 and len(base) <= _INITIALS_MAXLEN) else w
        if not first and w.lower() in _PARTICLES:      # nobiliary particle
            return w.lower()
        return (w.capitalize() if (len(w) >= _CASEFIX_MINLEN
                                   and (w.isupper() or w.islower())) else w)

    return " ".join(_fix(w, i == 0) for i, w in enumerate(n.split()))
