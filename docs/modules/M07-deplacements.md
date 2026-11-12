# M7 — Déplacements et temps ST / LIRMM

## Objectif
Planifier les séjours au LIRMM et documenter la répartition contractuelle du temps
entre ST et le laboratoire (exigence CIFRE).

## Modèles
- `Stay(thesis, location: ROUSSET|MONTPELLIER|CONFERENCE|OTHER, start_date, end_date,
  purpose, mission_order_status: NOT_NEEDED|TO_REQUEST|REQUESTED|APPROVED,
  transport: TRAIN|BIKE|BUS|PLANE|OTHER, lodging_note, cost_estimate)`
- `TimeSplitTarget(thesis, year, st_percent, lab_percent)` — depuis le contrat de
  collaboration.

## Fonctionnalités
1. Calendrier des séjours (vue mois) ; export iCal (dates et lieu seulement).
2. Compteur de jours ST / LIRMM par année CIFRE comparé à la cible, alerte si
   l'écart dépasse 10 points.
3. Liste de préparation par séjour : ordre de mission ST, billet de train,
   hébergement, badge ZRR valide.
4. Pas de voiture : transport par défaut `TRAIN`, avec option vélo sur place.

## Critères d'acceptation
- [ ] Le calcul des jours exclut week-ends et jours fériés français (test).
- [ ] L'écart à la cible est correct sur une année CIFRE à cheval sur deux années
      civiles (test).
- [ ] Le tuteur ST voit les séjours ; un co-encadrant voit seulement ceux à
      Montpellier.
