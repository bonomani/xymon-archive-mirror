# Fidelity check: mirror vs original

One footer-bearing message sampled per year (2005–2026). The **original** body
is reconstructed from the stored raw mbox bytes (the same source the live
Pipermail site renders), run through the full render pipeline, and diffed
against the mirror's output. Matching is punctuation-insensitive so linkified
URLs (`<http://x>` → `http://x`) don't read as differences.

## Result: 0 / 22 years lose real content

Every removed line is boilerplate, in one of three categories:

| Category | Example | Why removing it is correct |
|---|---|---|
| List footer | `Xymon mailing list` / `Xymon at xymon.com` / `…/listinfo/xymon` | list-injected, not message content |
| Unsubscribe footer | `To unsubscribe from the hobbit list, send an e-mail to …` | list-injected |
| Pipermail scrub-note | `---- next part ----`, `An HTML attachment was scrubbed`, attachment `Name/Type/Size/Desc/URL:` | placeholder text; the real attachment is recovered into the Attachments box / inlined |

Preserved verbatim: prose, code/config/commands, quoted replies (kept, muted),
and all links (linkified). Email addresses are pseudonymised by design.

Reproduce: `python3 /tmp/compare2.py` (see git history of this analysis).
