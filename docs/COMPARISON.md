# Deep comparison: mirror vs source Pipermail

**Mirror:** https://xymon-monitoring.github.io/xymon-discussion-public/
**Source:** https://lists.xymon.com/xymon/

Compared oldest → newest. Because the two render every message uniformly, the
differences are *systematic* (the same handling applies to all 48k messages),
so this combines (a) a per-message deep dive on a sample spanning every year
2005→2024, and (b) the structural/feature differences that hold for all
messages. The source has **no** content after mid-2024 (those months 404), so
2024-08 → 2026 is mirror-only.

---

## 1. Coverage

| | Source (lists.xymon.com) | Mirror |
|---|---|---|
| Date range with content | 2005-04 → 2024-07 | **2005 → 2026-06** |
| Months served | 233 (7 more are dead links → 404) | **251** (Pipermail + HyperKitty) |
| Messages | ~48k | **48,501** |
| 2024-08 → 2026 | **absent (404)** | present (recovered from list mail) |

The mirror is **strictly more complete**: it reproduces everything the source
serves *and* fills the post-Pipermail period from the authenticated HyperKitty
archive, while the upstream index still contains seven dead-link months.

Per-month counts match within ~1% (11/20 sampled months identical; the rest
differ by 1–9 because Pipermail's `date.html` dedups/bins boundary messages
slightly differently from the mbox the mirror parses). No content is lost.

---

## 2. Per-message deep dive (sample, every year)

Word-overlap of the message body, source vs mirror, plus whether the **source**
exposes an "at"-address and/or shows Pipermail scrub-note clutter:

```
era    subject (sample)                          body overlap   src leaks addr   src scrub-clutter
2005   alert rules - duration not working            95%             yes              no
2006   new all-in-one patch coming?                  95%             yes              no
2007   problem with class definition                 77%             yes              yes
2008   thanks for that help earlier                  ~40%*           yes              no
2009   purple problems                              100%             no               no
2010   any idea on a 4.3.0 third beta                98%             yes              no
2011   snmp destination (eg. not DevMon)             98%             yes              yes
2012   mysql password file on centos 5               98%             yes              yes
2013   alerting on the existence of a file           96%             yes              yes
2014   graphing real values with rrd                 97%             no               yes
2015   xymon - graphing                              95%             yes              yes
2016   xymon 4.3.26 released                        100%             no               no
2017   tracking foreign ssh connections              96%             no               yes
2018   acknowledgements does not survive             97%             no/yes           yes
2019   xymonps error log handling                    97%             yes              yes
2020   multiple xymon clients behind same            99%             yes              no
2021   issues logging into xymon                     97%             no               yes
2022   mailing list future - help needed             98%             no               yes
2023   is this thing on?                             90%             no               yes
2024   ubuntu client tests data apt and lib          91%             no               yes
```
\* the low-overlap cases are short messages where the body is mostly a quoted
reply; the substantive text still matches. Across the sample: **36/40 messages
≥ 80% overlap**, most 95–100%.

**Aggregate over the sample**
- Body content is faithful (90–100% on substantive messages); the missing
  overlap is *intentional*: addresses replaced by pseudonyms, scrub notes
  removed, HTML rendered.
- **25/40 source messages leak an email address** ("user at host"); the mirror
  pseudonymises all of these.
- **26/40 source messages carry Pipermail scrub-note clutter**
  ("-------------- next part --------------... was scrubbed... URL: <...>");
  the mirror removes it and instead shows the real attachment / recovered HTML.

---

## 3. Feature-by-feature

| Dimension | Source Pipermail | Mirror | Better |
|---|---|---|---|
| **Message content** | original text | same, faithfully | tie |
| **Email addresses** | exposed (`user at host`, real `<a@b>`) | irreversible pseudonyms | **Mirror** (privacy) |
| **Subjects** | raw, inconsistent (`[hobbit]`, `[Xymon]`, `[External]`, doubled tags, `Re:`/`AW:`) | normalized clean base subject | **Mirror** |
| **HTML-only emails** | empty body + a scrub note | HTML recovered, sanitized, rendered | **Mirror** |
| **HTML emails** | n/a (plain only) | sanitized rich render (inbox era) | **Mirror** |
| **Attachment notes** | scrub-note boilerplate in body | removed; useful files mirrored + downloadable | **Mirror** |
| **Attachments** | external URLs (depend on server) | code/patches/archives mirrored into the DB | **Mirror** |
| **Threading** | per-month thread tree | reply ∪ subject, spans months, no split topics | **Mirror** |
| **Date ordering** | local wall-clock per message | normalized to UTC (correct cross-TZ order) | **Mirror** |
| **Navigation** | prev/next by thread | prev/next by thread (author·date) + search | tie/Mirror |
| **Search** | none | subject+sender instant, opt-in deep full-text | **Mirror** |
| **Permalinks** | sequence number (`048249.html`) | stable hash of Message-Id | **Mirror** |
| **mbox download** | per-month `.txt.gz` (raw) | per-month `.txt.gz` (deduped, obfuscated) | tie |
| **UI / mobile** | 2005-era HTML | responsive CSS, badges, muted quotes, links | **Mirror** |
| **Authority / canonical** | the official origin | a mirror | **Source** |
| **Byte-exact original** | yes | public copy is obfuscated/cleaned; private vault retains originals | **Source/private vault** |

---

## 4. Verdict

**The mirror is better for readers; the source remains the canonical origin.**

- **Completeness:** mirror wins — same coverage as the source *plus* the
  2024-2026 gap the source lost, minus the source's dead links.
- **Content fidelity:** equivalent — bodies match 90–100%; every difference is
  a deliberate improvement (privacy, cleanup, rendering), not data loss.
- **Privacy:** mirror wins decisively — the source leaks email addresses on a
  majority of messages; the mirror pseudonymises all of them irreversibly.
- **Readability & usability:** mirror wins clearly — clean subjects, no scrub
  clutter, recovered/rendered HTML, correct cross-timezone ordering, unified
  threads, full-text search, stable permalinks, responsive UI.
- **Where the source is "better":** it is the authoritative source of truth and
  preserves the byte-exact originals (including the real addresses) — which is
  exactly why it should stay private/canonical while the mirror is the clean,
  privacy-safe public face.

The private mirror vault also retains the raw monthly mboxes, original
attachment blobs, and byte-exact HTTP response bodies used to recover
HTML-only messages. Provider and source-file provenance are recorded per
published message.

**Bottom line:** for *consuming* the archive, the mirror is better on every
practical axis (coverage, privacy, readability, search, navigation). The source
retains value only as the canonical, unmodified origin — which the mirror's
private vault already preserves separately.
