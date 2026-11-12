# M5 — Réunions et présentations

## Objectif
Planifier les points, comités et présentations ; garder ordres du jour, comptes
rendus, décisions et actions.

## Modèles
- `Meeting(thesis, kind: WEEKLY|STEERING|CSI|ST_TEAM|LIRMM_TEAM|OTHER, title,
  start, end, location: ROUSSET|MONTPELLIER|VISIO|OTHER, visio_link, attendees
  (M2M Membership), agenda, minutes (Document chiffré), confidentiality)`
- `Decision(meeting, text, decided_by)` — alimente le registre des décisions.
- `ActionItem(meeting, title, assignee, due_date, status, linked_objective)`
- `Presentation(thesis, title, venue: SEMINDOCS|ST_TEAM|CONFERENCE|CSI|OTHER,
  date, slides (Document), status)`

## Fonctionnalités
1. Création de réunion ; export `.ics` par réunion et flux iCal personnel signé
   (URL à jeton révocable) — le flux ne contient que titre, date et lieu.
2. Gabarit d'ordre du jour pré-rempli : actions ouvertes + objectifs en retard.
3. Compte rendu rédigé dans la plateforme, stocké chiffré (M2).
4. Récurrence simple (hebdomadaire) pour les points.
5. Registre des décisions consultable et filtrable.

## Critères d'acceptation
- [ ] Le flux iCal ne contient aucun compte rendu ni ordre du jour (test).
- [ ] Le jeton du flux iCal est révocable ; ancien jeton → 404 (test).
- [ ] Un invité `GUEST` ne voit que la réunion à laquelle il est convié.
- [ ] Une action liée à un objectif apparaît sur la fiche de l'objectif.
