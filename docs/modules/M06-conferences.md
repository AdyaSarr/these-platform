# M6 — Conférences et deadlines

## Objectif
Toutes les conférences du domaine, leurs deadlines et les objectifs de soumission,
avec le délai de relecture ST décompté à rebours.

## Modèles
- `Venue(acronym, name, kind: CONFERENCE|JOURNAL|WORKSHOP, url, core_rank, topics)`
- `Edition(venue, year, abstract_deadline, paper_deadline, rebuttal_start,
  notification, camera_ready, event_start, event_end, location, source,
  last_synced_at)` — fuseau AoE (UTC−12) stocké explicitement.
- `SubmissionPlan(thesis, edition, paper_title, objective (FK M4), st_review_days
  (défaut 30), internal_deadline (calculée), status: IDEA|WRITING|ST_REVIEW|
  SUBMITTED|ACCEPTED|REJECTED|WITHDRAWN, budget_estimate, notes)`

## Sources de données
- Flux iCalendar IACR (« ordered by date of submission ») via `icalendar`.
- Fichier YAML du dépôt `sec-deadlines` (licence MIT).
- Saisie manuelle toujours possible ; une saisie manuelle n'est jamais écrasée par
  une synchronisation.
- Tâche Celery quotidienne ; requêtes HTTP avec délai max, taille max, et
  validation stricte du contenu (données externes = non fiables).

## Liste initiale (à vérifier)
TCHES/CHES, CASCADE, PQCrypto, Real World Crypto (+ RWPQC), Eurocrypt, Crypto,
Asiacrypt, PKC, ACNS, DATE, Journées C2.

## Critères d'acceptation
- [ ] `internal_deadline = paper_deadline − st_review_days` (test), alerte à J-7.
- [ ] Les dates AoE sont converties correctement en heure de Paris (tests).
- [ ] Une synchronisation n'écrase pas un champ saisi à la main (test).
- [ ] Un flux malformé ou trop gros est rejeté sans planter la tâche (test).
