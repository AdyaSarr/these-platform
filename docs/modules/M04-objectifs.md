# M4 — Objectifs et indicateurs

## Objectif
Des objectifs par année de thèse, qui peuvent évoluer, mis à jour par le doctorant,
avec des statistiques de suivi visibles par les encadrants.

## Modèles
- `ThesisYear(thesis, number, cifre_start, cifre_end, academic_start, academic_end)`
- `Objective.year` désigne l'**année CIFRE** (horloge des 36 mois, depuis la date
  d'effet). Les rapports CSI (M8) sélectionnent par dates dans l'année
  universitaire (`docs/decisions.md`).
- `Objective(thesis, year, title, description, success_criterion, category:
  RESEARCH|PUBLICATION|TRAINING|ADMIN|OTHER, due_date, weight (1–3), status:
  PLANNED|IN_PROGRESS|ACHIEVED|PARTIAL|NOT_ACHIEVED|DROPPED, progress (0–100),
  approved_by, approved_at)`
- `ObjectiveRevision(objective, changed_by, changed_at, field, old_value,
  new_value, reason)` — **historique obligatoire** ; un objectif abandonné reste
  visible avec sa raison.
- `ProgressUpdate(objective, date, progress, note, evidence (Document facultatif))`

## Fonctionnalités
1. Le doctorant propose les objectifs d'une année ; le directeur les approuve
   (ré-authentification) ; ensuite toute modification de titre, critère ou
   échéance exige une raison et crée une révision.
2. Mises à jour d'avancement datées, avec preuve facultative. Un changement de
   **résultat** (`ACHIEVED`, `PARTIAL`, `NOT_ACHIEVED`, déclaré ou retiré) d'un
   objectif approuvé compte aussitôt dans les statistiques, mais l'objectif est
   marqué « résultat déclaré, à prendre en connaissance » jusqu'à la prise de
   connaissance du directeur (même mécanisme que « modifié depuis
   l'approbation » : second facteur récent, journalisée) ; il figure dans son
   résumé hebdomadaire (`services.pending_acknowledgements`).
3. Encadrants : commentaires sur un objectif (fil simple).
4. Tableau de bord : taux d'atteinte pondéré par année, objectifs en retard,
   évolution mensuelle (graphique simple, données agrégées côté serveur),
   répartition par catégorie.
5. Résumé hebdomadaire envoyé aux encadrants (lien seulement).

## Critères d'acceptation
- [ ] Toute modification d'un objectif approuvé sans raison est refusée (test).
- [ ] Le taux d'atteinte pondéré est calculé correctement (tests unitaires sur
      `services.compute_stats`).
- [ ] Un objectif `DROPPED` apparaît dans les statistiques comme abandonné, pas
      supprimé.
- [ ] Un membre du CSI voit les objectifs en lecture seule, sans les commentaires
      internes.

## Notes issues de M1
- Droits (`apps/accounts/permissions.py`) : `OBJECTIVE_READ` donne accès aux
  objectifs **mais pas à leur fil de commentaires**. Les commentaires sont internes
  à l'encadrement et ne s'affichent qu'aux rôles ayant `OBJECTIVE_COMMENT`
  (doctorant, directeur, co-encadrants, tuteur). Le `CSI_MEMBER` a
  `OBJECTIVE_READ` seulement : il voit les objectifs en lecture seule, sans les
  commentaires. Le filtrage se fait dans la vue et le service (`can(...,
  OBJECTIVE_COMMENT, ...)`), jamais seulement dans le gabarit.
