# M7 — Déplacements et temps ST / LIRMM

## Objectif
Planifier la présence du doctorant (ST Rousset, LIRMM, télétravail, conférence,
congé) et ses missions, suivre les ordres de mission, les frais et les budgets, et
documenter la répartition contractuelle du temps entre ST et le laboratoire
(exigence CIFRE).

Écarts à la première version de la fiche (plan validé le 2026-11-15) :
- `Stay` remplacé par un **calendrier jour par jour** (`PresenceDay`) et des
  **missions** (`Mission`) distinctes ; le motif d'une mission se lie à une réunion
  M5 ou à une soumission M6.
- Ajouts : ordre de mission avec décision par la personne du payeur, frais estimés et
  réels scellés, justificatifs dans le coffre, budget annuel par payeur (reporté de
  M6), rappels, règle mensuelle facultative de jours au LIRMM.
- Gardés : compteur ST / LIRMM par année CIFRE contre une cible, alerte au-delà de
  10 points ; liste de préparation (le badge ZRR renvoie à la pièce M3
  `fsd_zrr_clearance`, rien n'est recopié) ; pas de voiture.
- Minimisation des données (RGPD) : chaque rôle ne voit que les types de jours dont
  il a besoin (voir « Droits »).
- **Aucune adresse personnelle** stockée, **aucun appel réseau** (pas d'API SNCF ou
  autre) : saisie manuelle.

## Modèles (`apps/travel/`)

Tous héritent de `core.TimeStampedModel`, clé primaire UUID, FK `thesis`. Rien n'est
supprimé (une mission s'annule).

- `PresenceDay(thesis, day, kind)` — `UNIQUE(thesis, day)` ; `kind` : `ROUSSET`,
  `LIRMM`, `REMOTE`, `CONFERENCE`, `LEAVE`. Jours entiers seulement. Saisie par
  plage (92 jours au plus) dans les bornes de la thèse ; un jour saisi peut être
  effacé (redevient « non renseigné »). Type et date en clair (pas de texte libre,
  le compteur en a besoin).
- `TimeSplitTarget(thesis, cifre_year, st_percent, lab_percent)` — `UNIQUE(thesis,
  cifre_year)`, entiers de 0 à 100, `st + lab = 100` ; issue du contrat de
  collaboration (M3), que la plateforme n'interprète pas.
- `TravelPolicy(thesis, …)` — une ligne par thèse :
  - règles de comptage : `remote_counts_as` (`ST` par défaut), `conference_counts_as`
    (`LAB` par défaut), `whit_monday_holiday` (vrai par défaut),
    `gap_alert_points` (10 par défaut, 1 à 50), `min_lab_days_per_month` (vide = pas
    de règle, 0 à 23) ;
  - rappels : `order_lead_days` (15 par défaut, 0 à 90), `receipt_grace_days` (3 par
    défaut, 0 à 30).
- `Mission(thesis, start_date, end_date, destination, edition, meeting, submission,
  transports, lodging, payer, level, status, checklist)`.
  - `destination` : `LIRMM`, `ROUSSET`, `CONFERENCE`, `OTHER` ; `edition` (M6)
    seulement pour `CONFERENCE` ; `meeting` (M5) et `submission` (M6) facultatifs,
    de la même thèse.
  - `transports` : un ou plusieurs parmi `TRAIN` (par défaut), `COACH`, `PLANE`,
    `BIKE`, `CARPOOL`, `URBAN_TRANSIT` ; aucune voiture personnelle.
  - `lodging` : `NONE`, `HOTEL`, `GUESTHOUSE`, `PRIVATE`, `OTHER` ; `PRIVATE` (chez
    un proche) ne stocke rien de plus, le formulaire avertit de n'écrire aucune
    adresse dans la note.
  - `payer` : `ST`, `LIRMM`, `OTHER`. **Payeur ⇒ niveau** : `ST` ⇒ {`PUBLIC`,
    `INTERNAL_ST`, `CONFIDENTIAL`}, `LIRMM` ⇒ {`PUBLIC`, `INTERNAL_LIRMM`,
    `CONFIDENTIAL`} (la personne qui décide lit ce qu'elle accorde) ; niveau par
    défaut `INTERNAL_ST` ou `INTERNAL_LIRMM`, `INTERNAL_LIRMM` pour `OTHER`.
  - `start_date <= end_date`, 60 jours au plus, bornes de `meeting_bounds`.
  - Statuts : `PLANNED`, `ORDER_REQUESTED`, `ORDER_GRANTED`, `ORDER_REFUSED`, `DONE`,
    `REIMBURSED`, `CANCELLED`. Payeur `OTHER` : aucun ordre dans la plateforme,
    `PLANNED → DONE`. `DONE` exige `end_date <=` date de Paris et, sauf `OTHER`, un
    ordre accordé qui couvre la mission ; `REIMBURSED` exige `DONE` et, sur chaque
    ligne de frais, un montant réel et un justificatif. Terminaux : `REIMBURSED`,
    `CANCELLED`.
  - Liste de préparation (booléens) : ordre de mission, billets, hébergement ; état
    de la pièce M3 `fsd_zrr_clearance` affiché pour une mission au LIRMM.
- `MissionOrder(mission, requested_by, payer, snapshot_start, snapshot_end,
  snapshot_destination, decision, reason, decided_by, decided_at)` — `decision` :
  `PENDING`, `GRANTED`, `REFUSED`, `WITHDRAWN` ; motif de refus en liste fermée
  (`BUDGET_EXHAUSTED`, `NOT_JUSTIFIED`, `MISSING_INFORMATION`, `DATES_CONFLICT`,
  `WRONG_PAYER`, `OTHER`) ; une seule demande en attente par mission ;
  `decided_by <> requested_by` ; décidée = jamais modifiée (historique).
- `Expense(mission, category)` — `TRANSPORT`, `LODGING`, `MEALS`, `REGISTRATION`,
  `OTHER` ; montants estimé et réel scellés.
- `ExpenseReceipt(expense, document)` — justificatif déposé dans le coffre au niveau
  de la mission.
- `TravelBudget(thesis, payer, year)` — `payer` ∈ {`ST`, `LIRMM`}, année civile,
  `UNIQUE(thesis, payer, year)` ; montant scellé au niveau fixé par le payeur
  (`INTERNAL_ST`, `INTERNAL_LIRMM`).
- `TravelNotification(kind, target_id, window, recipient, …)` — unicité par (type,
  cible, fenêtre, destinataire) ; destinataire revérifié à l'envoi.

## Temps ST / LIRMM

- Jours ouvrés : du lundi au vendredi, hors jours fériés de métropole (1er janvier,
  lundi de Pâques, 1er et 8 mai, Ascension, lundi de Pentecôte si
  `whit_monday_holiday`, 14 juillet, 15 août, 1er et 11 novembre, 25 décembre) ;
  Pâques par l'algorithme grégorien anonyme (`apps/travel/holidays.py`, aucune
  dépendance, aucun appel réseau).
- Côté ST : `ROUSSET`, et `REMOTE` / `CONFERENCE` selon la politique ; côté
  laboratoire : `LIRMM`, et `REMOTE` / `CONFERENCE` selon la politique. Exclus :
  `LEAVE`, jours non renseignés, jours non ouvrés.
- Par année CIFRE (`objectives.years`), à date ou sur l'année entière : part ST =
  jours ST / (jours ST + jours labo), en fraction exacte ; écart = |part ST − cible
  ST| en points ; alerte si l'écart **dépasse** `gap_alert_points`.
- Règle mensuelle : nombre de jours `LIRMM` du mois ≥ `min_lab_days_per_month`.

## Textes scellés (plage 0x04xx)

| Code | Emplacement |
|---|---|
| 0x0401 | `travel.Mission.destination_detail` (lieu d'une destination `OTHER`) |
| 0x0402 | `travel.Mission.purpose` |
| 0x0403 | `travel.Mission.lodging_note` |
| 0x0404 | `travel.MissionOrder.comment` |
| 0x0405 | `travel.MissionOrder.estimated_total` (estimation figée à la demande) |
| 0x0406 | `travel.Expense.estimated_amount` |
| 0x0407 | `travel.Expense.actual_amount` |
| 0x0408 | `travel.TravelBudget.amount` |

0x0401 à 0x0407 au niveau de la mission ; un changement de niveau rescelle tout et
change le niveau des justificatifs. **Les montants sont scellés** (données
financières de ST et du LIRMM) : euros seulement, forme canonique `"1234.50"`,
saisie `^\d{1,6}([.,]\d{1,2})?$` ; les totaux se calculent en Python après un seul
`open_texts` par requête.

## Ordre de mission

- **Demande** (doctorant) : mission `PLANNED` ou `ORDER_REFUSED`, payeur `ST` ou
  `LIRMM`, au moins une personne active qui peut décider et lire le niveau ; fige
  dates, destination, payeur et l'estimation totale (scellée).
- **Décision** (`VALIDATE_MISSION_ORDER` et rôle du payeur : tuteur pour `ST`,
  directeur pour `LIRMM`, qui lit le niveau ; second facteur de moins de
  10 minutes, demandé après le contrôle des droits) : `GRANTED` ou `REFUSED` (motif
  obligatoire), commentaire scellé facultatif. Journalisée, doctorant notifié (lien
  seulement).
- **Retrait** d'une demande en attente par le doctorant (journalisé).
- **Couverture** : un ordre accordé ne couvre la mission que si son instantané
  correspond encore à ses dates, sa destination et son payeur. Changer l'un d'eux
  sur une mission `ORDER_REQUESTED` est refusé (409 `order_pending`) ; sur une
  mission `ORDER_GRANTED`, la mission revient à `PLANNED` (`MISSION_ORDER_INVALIDATED`).

## Droits

| Action | Doctorant | Directeur | Co-encadrant | Tuteur | CSI | Invité |
|---|---|---|---|---|---|---|
| `TRAVEL_READ` (dates, types, statuts ; textes et montants selon le niveau) | ✓ | ✓ | ✓ (LIRMM) | ✓ | — | — |
| `TRAVEL_WRITE` (présence, missions, frais, justificatifs, demandes) | ✓ | — | — | — | — | — |
| `TRAVEL_REMINDERS_SET` (`order_lead_days`, `receipt_grace_days`) | ✓ | — | — | — | — | — |
| `TRAVEL_RULES_SET` (cible, règles de comptage, règle mensuelle) | — | ✓ | — | ✓ | — | — |
| `TRAVEL_BUDGET_SET` (budget `LIRMM` : directeur ; `ST` : tuteur) | — | ✓ | — | ✓ | — | — |
| `VALIDATE_MISSION_ORDER` (`LIRMM` : directeur ; `ST` : tuteur) | — | ✓ | — | ✓ | — | — |

**Minimisation (`visible_to(user)`, testé par rôle)** :
- doctorant, tuteur (employeur) : tous les types de jours, congés compris ;
- directeur : `ROUSSET`, `LIRMM`, `REMOTE`, `CONFERENCE` ; un `LEAVE` apparaît comme
  « indisponible », sans le mot congé ; toutes les missions ;
- co-encadrant : seulement les jours `LIRMM` (les autres sont indiscernables d'un
  jour non renseigné) et les missions vers le LIRMM ;
- **le compteur affiché à chacun ne permet pas de retrouver le détail masqué** : le
  directeur voit les totaux de ce qu'il voit jour par jour (« indisponible » compris),
  le co-encadrant seulement ses jours `LIRMM` et la règle mensuelle, ni part ST, ni
  écart, ni total ST.

Second facteur récent et journal (ancienne → nouvelle valeur, **jamais un
montant**) : décider un ordre, changer le niveau d'une mission, régler la cible, les
règles, les rappels, un budget. Régler la cible ou les règles notifie l'autre
encadrant (directeur ou tuteur) et le doctorant, par lien seulement.

**Jour passé** (date < aujourd'hui à Paris) : toute modification ou tout effacement
est journalisé (`PRESENCE_CORRECTED` : date, ancien type, nouveau type) ; les jours
à venir se modifient sans journal.

## Rappels (un lien seulement, règle 7)

Commande quotidienne `travel_reminders` (timer systemd) ; fenêtres en dates de
Paris :
- ordre à demander (doctorant) : J-(N+7) et J-N avant le départ, N =
  `order_lead_days`, mission `PLANNED` ou `ORDER_REFUSED` d'un payeur `ST` ou `LIRMM` ;
- ordre en attente (personne qui décide) : J-7 et J-1 avant le départ ;
- justificatifs manquants (doctorant) : J+`receipt_grace_days` et J+14 après le
  retour, mission `DONE` avec une ligne de frais sans montant réel ou sans
  justificatif.

Événements : demande (personne qui décide), décision (doctorant), cible ou règles
réglées (voir « Droits »).

## Export `.ics`

Par mission et pour toutes les missions visibles par l'utilisateur : événements sur
des journées entières (`VALUE=DATE`, fin exclusive), libellé de la destination
seulement (« Mission — LIRMM Montpellier », « Conférence », « Déplacement »),
statut, lien. Jamais de motif, de montant, de lieu `OTHER`, de nom de conférence ni
d'hébergement.

## Liens et tableau de bord

- Carte « Déplacements » (`TRAVEL_READ`) : prochaine mission, ordres en attente
  (pour la personne qui décide), justificatifs manquants, écart à la cible et règle
  du mois (selon la minimisation), budget consommé si son niveau est lisible.
- Section « Déplacements » sur le détail d'une réunion (M5) et d'une soumission
  (M6), avec « Prévoir une mission » prérempli.

## Critères d'acceptation
- [ ] Le calcul des jours exclut week-ends et jours fériés français, lundi de
      Pentecôte selon la politique (test).
- [ ] L'écart à la cible est correct sur une année CIFRE à cheval sur deux années
      civiles (test).
- [ ] Le tuteur voit tous les types de jours ; le directeur voit un congé comme
      « indisponible » ; un co-encadrant voit seulement les jours et les missions
      LIRMM ; aucun compteur ne révèle le détail masqué (tests par rôle).
- [ ] Seule la personne du payeur décide un ordre de mission, avec un second facteur
      récent demandé après le contrôle des droits ; un ordre dont l'instantané ne
      correspond plus ne couvre pas la mission (tests).
- [ ] Toute modification d'un jour passé est journalisée (`PRESENCE_CORRECTED`) ;
      un jour à venir, non.
- [ ] Aucun montant, motif, lieu `OTHER`, note d'hébergement ou commentaire en clair
      en base, dans les journaux, les courriels ou les `.ics`.
- [ ] Aucun champ d'adresse, aucun import réseau dans `apps/travel` (tests statiques).
- [ ] Rappels aux bonnes fenêtres, sans doublon, un lien seulement.
- [ ] `demo_seed` : présence fictive, missions fictives (LIRMM, conférence M6), un
      ordre accordé, un en attente, un refusé, montants fictifs.
