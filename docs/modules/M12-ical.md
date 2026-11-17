# M12 — Personal iCal feed with a token

Plan validated on 2026-11-17.

## Purpose

A subscription from Calendar (iPhone) or another client to a personal feed for
each (person, thesis): one's meetings, the due dates that concern one, missions,
objective due dates. **Threat context**: the URL is a bearer secret, with no
second factor; the content is read by a third-party application and often by the
provider's servers (iCloud); the URL can leak (screenshot, proxy log,
mistaken sharing). Hence a minimal content, a revocable token that is hashed and
time-limited, and journals that never contain the token.

## Model (`apps/feeds/`)

`CalendarFeed(id UUID, user, thesis, token_hash unique, created_at, expires_on,
revoked_at, last_used_on)`. At most **one live feed per (person, thesis)**
(partial unique constraint `revoked_at IS NULL`).

## Token

- `secrets.token_urlsafe(32)` (256 bits); only `vault.crypto.hash_token`
  (SHA-256) is stored; lookup by fingerprint, then `tokens_match` (constant
  time).
- **Disabled by default**; **creating** and **regenerating** require a
  re-authentication less than 10 minutes old (rule 6). **Revoking** does not
  require one (it reduces access and is useful in an emergency): a `POST` with
  CSRF, journaled.
- URL **shown once**, on the `no-store` response page of the creation (or the
  regeneration); then only its existence, its dates and its last use.
- **Expiry**: 12 months (`expires_on`); an « Échéances » notice 14 days before
  (activity event `CALENDAR_FEED_EXPIRING`, a link only); expired = revoked,
  journaled by the daily run.
- **Automatic revocation**: departure from the team (`end_membership`), change of
  role (`change_role`), thesis `DEFENDED` or `CLOSED` (daily run; no service moves
  a thesis to those statuses). On every request the lookup requires anyway an
  active membership, an allowed role, an open thesis, a feed neither revoked
  nor expired (defence in depth).
- **Journal**: `CALENDAR_FEED_CREATED`, `CALENDAR_FEED_REGENERATED`,
  `CALENDAR_FEED_REVOKED` with `reason ∈ {user, membership_ended, role_changed,
  thesis_closed, expired}`; never the token nor its fingerprint; nothing per
  request. `last_used_on`: the day, updated at most once a day.

## Rights

Allowed: `PHD_STUDENT`, `DIRECTOR`, `CO_SUPERVISOR`, `INDUSTRIAL_TUTOR`,
`INDUSTRIAL_CO_SUPERVISOR`. Excluded: `CSI_MEMBER`, `GUEST`. Content = what the
person reads (the existing rules, option B), narrowed to what concerns them.

## Content (window: 30 days back, 12 months ahead)

| Object | SUMMARY | LOCATION | DESCRIPTION / URL | Included if |
|---|---|---|---|---|
| Meeting (M5a) | type label (`meetings.ics.summary`) | location label | « Détails sur la plateforme » + link to the meeting | the person is an invitee and reads the level |
| M3 piece | « Pièce du dossier administratif à déposer » / « … à valider » | — | idem, link to the piece | student: `TODO` or `REJECTED`; validator who has not approved: `SUBMITTED`; section read |
| M6 deadline | the deadline's type label only (never the conference) | — | idem, link to the submission | a live submission the person reads (list rule and level) aims at the edition |
| M7 mission | `travel.ics.summary` | — | idem, link to the mission | `Mission.objects.visible_to` |
| Objective due date | « Échéance d'objectif (<catégorie>) » | — | idem, link to the objective | `OBJECTIVE_READ` and readable level, approved and not closed |

**Never**: a sealed text (title, agenda, minutes, purpose, place detail, visio
link; no `SEALED_TEXT_READ`), a label written by the student, a
catalogue label, a conference name, a level, an amount, a person (`ATTENDEE`,
`ORGANIZER`), presence days and leave, documents, budgets.

## ICS format

The M5a writer (`escape`, `fold`, `CRLF`) and M7's summaries move to
`apps/core/ics.py`; **the existing export tests stay green without
modification**. Stable UIDs, identical to those of the existing exports
(`<meeting>@host`, `mission-<uuid>@host`; `item-`, `deadline-`, `objective-` for
the new ones). `SEQUENCE`: `ics_sequence` (meetings, missions), 0 elsewhere.
`STATUS:CANCELLED` for a cancelled meeting or mission within the window. Times
in **UTC (`Z`)**, without `VTIMEZONE`; due dates and missions all-day
(`VALUE=DATE`). RFC 5545 escaping and **refusal of any control character**
(CR, LF included) in a value, before escaping.

## HTTP

`GET` (and `HEAD`) `/ical/<jeton>.ics`, outside `/t/`. Unknown token, revoked,
expired, thesis closed, membership ended or role excluded: **a single query, the same
`404`, same body, same headers**. No cookie read or set (the view
touches neither the user nor the session; route exempted from the MFA middleware). Headers:
`Cache-Control: private, no-store`, `X-Robots-Tag: noindex, nofollow`,
`Referrer-Policy: no-referrer`, `Content-Type: text/calendar; charset=utf-8`.
Rate limit **60/h per token** (fingerprint) and **300/h per address**
(pseudonym), counted **before** the lookup: the same `429` for a valid token or
not.

## Journals

A `TokenPathFilter` filter on every handler replaces `/ical/<jeton>.ics` with
`/ical/[jeton].ics` (message, arguments, request); the view's responses
are marked as already journaled (no « Not Found » line from Django);
the error reports mask the path; no Celery task. Instruction for the
proxy in `docs/DEPLOYMENT.md`.

## Interface

« Abonnement agenda » block on « Mes notifications » (per thesis): explanation of
the bearer secret; « Créer le lien » (re-authentication); the URL once (field,
`webcal://` link), the instructions **recommended**: *Réglages › Calendrier › Comptes
› Ajouter un compte › Autre › Ajouter un calendrier avec abonnement*, and: « un
abonnement ajouté dans iCloud (depuis un Mac ou iCloud.com) est relu par les
serveurs d'Apple ; préférez l'ajout sur l'appareil » ; then the dates (creation,
expiry, last use to the day), « Régénérer » (re-authentication) and « Révoquer »
(without), behind `<details>` confirmations.

## Acceptance criteria

1. Token of 256 bits, fingerprint only in the database (no table holds the token),
   one live feed per (person, thesis), enforced by the database.
2. Creation and regeneration: recent re-authentication required; revocation
   without one, `POST` with CSRF; all three journaled with codes only.
3. URL shown once; regeneration: the old one gives 404 at once.
4. Automatic revocation (departure, role, thesis closed, expiry) and notice
   « Échéances » 14 days before expiry.
5. Two worlds: for each restricted role, the `.ics` is identical byte for byte
   when only objects it does not read differ; checked by mutants.
6. `TEMOIN-FUITE-…` witness in a sealed value and in a clear-text label written by
   the student: absent from every feed; no `SEALED_TEXT_READ`.
7. The token is absent from every journal (Django, `django.request`, `runserver`,
   error reports, audit); the filter is checked by a mutant.
8. Indistinguishable refusals (same status, body, headers, one query); no
   `Set-Cookie`, session not loaded; headers present; 405 other than GET/HEAD;
   429 identical.
9. Control characters refused; escaping and folding checked.
10. One allowed test and one refused test per role for each view.
11. **The existing tests of the M5a and M7 `.ics` exports stay green without
    modification** after the move to `core/ics.py`.
