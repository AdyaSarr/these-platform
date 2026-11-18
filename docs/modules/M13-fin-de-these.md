# M13 — Fin de thèse

Découpage validé le 2026-11-18 : **M13a** (cycle de vie, gardes de statut, accès)
maintenant ; **M13b** (export complet par le doctorant) et **M13c** (conservation
après `CLOSED`, destruction par effacement des clés puis des lignes) plus tard. M13c
attend les réponses Q8 à Q10 et un module « fournisseur de clés OpenBao » préalable
(`docs/ROADMAP.md`). Questions réglementaires ouvertes, sans réponse :
`docs/specs/questions-fin-de-these.md`.

# M13a — Cycle de vie, gardes de statut, accès

Plan validé le 2026-11-18. App `apps/lifecycle`.

## Objectif

Faire passer une thèse en `DEFENDED` puis en `CLOSED` (ou directement en `CLOSED` en
cas d'abandon), avec des confirmations proportionnées. Fixer ce qui reste
modifiable à chaque statut, et le faire respecter **à un seul endroit**, sans
qu'un service d'écriture puisse y échapper. Mettre fin aux accès après la clôture.

## Ce qui existait (avant M13a)

- `Thesis.Status` (`SETUP`, `ACTIVE`, `DEFENDED`, `CLOSED`) et `is_open` (`SETUP`,
  `ACTIVE`) : `apps/accounts/models.py`. Seul passage codé : `SETUP → ACTIVE` (M3,
  `THESIS_ACTIVATED`).
- `OPEN_THESIS = (SETUP, ACTIVE)`, recopié dans M3, M4, M5a, M6 et M7 (services et
  rappels) : leurs écritures sont gelées hors `SETUP` et `ACTIVE`.
- `core.delivery` saute tout courriel d'une thèse non ouverte (`thesis_closed`) ;
  `activity.digests` ne traite que les thèses ouvertes ; M12 révoque les flux iCal
  d'une thèse non ouverte.
- **Trous** : M2 (`store`, `add_version`, `change_level`, droits d'accès, textes
  scellés) et M1 (invitations, changement de rôle, prolongation, ajout
  d'adhésion, acceptation d'invitation) ne regardaient pas le statut.

## Garde de statut : dans `can()`

Le statut est une condition **de `permissions.py`**, pas des modules :

- Les actions se partagent en **lecture** (`READ_ACTIONS` : `VIEW_THESIS`,
  `VIEW_TEAM`, `*_READ`) et **écriture** (toutes les autres). Une lecture ne dépend
  jamais du statut.
- `WRITES_BY_STATUS` dit quelles écritures restent permises :

| Statut | Écritures permises (en plus du droit du rôle) |
|---|---|
| `SETUP`, `ACTIVE` | toutes |
| `DEFENDED` | M2 : `DOCUMENT_WRITE`, `DOCUMENT_COMMENT`, `DOCUMENT_GRANT` ; M6 : `CONFERENCE_WRITE`, `SUBMISSION_WRITE`, `VALIDATE_PUBLICATION` (l'accord de publication ST reste dû après la soutenance) ; M7 : `TRAVEL_WRITE` (réduit par M7, ci-dessous) ; M1 : `REMOVE_MEMBER` ; cycle de vie : `DECLARE_DEFENSE`, `REQUEST_CLOSURE` |
| `CLOSED` | aucune |

- `can()` et `allowed_actions()` lisent le statut **dans la même requête** que les
  rôles (jamais l'objet `Thesis` en mémoire, qui peut être périmé). Une écriture
  refusée pour le statut est donc **un refus de droit**, par construction : même
  exception (`PermissionDenied`), même ligne de journal (`*_ACCESS_DENIED`, raison
  `missing_right`), même 403. Les boutons disparaissent d'eux-mêmes (les pages
  lisent `allowed_actions`).
- **Lecture des commentaires d'objectifs** : `OBJECTIVE_COMMENT` servait aussi de
  droit de lecture du fil de commentaires (M4, fil d'activité). Nouvelle action de
  lecture `OBJECTIVE_COMMENT_READ`, tenue par les mêmes rôles ; les lectures
  passent par elle, l'écriture garde `OBJECTIVE_COMMENT`. Sans cela, une thèse
  close aurait caché ses commentaires à ceux qui les lisaient.
- Les gardes `OPEN_THESIS` des modules restent (défense en profondeur), lues dans
  `apps/accounts/statuses.py` (ci-dessous). M6 écrit en `DEFENDED`. M7 :
  `add_expense`, `update_expense` et `attach_receipt` passent en `DEFENDED`
  **seulement** pour une mission commencée avant la date de soutenance
  (`start_date < defended_on`) ; tout le reste de M7 reste gelé.
- M1, hors `can()` : `add_membership` refuse une thèse non ouverte
  (`PermissionDenied`) ; `ensure_usable` refuse une invitation d'une thèse non
  ouverte (`InvalidInvitationError`, comme une invitation expirée).

### Contrôles statiques

1. **Une seule liste de statuts** : aucun code hors de `apps/accounts/statuses.py`
   ne nomme une liste de statuts de thèse ; chaque module concerné lit la table.
2. **Matrice service × statut** (`tests/test_status_matrix.py`) : chaque fonction
   publique de chaque `apps/*/services.py` est classée (lecture, lecture
   journalisée, personnelle, système, réduction d'accès, écriture). La liste est
   fermée : une fonction nouvelle non classée fait échouer le test. Une fonction
   classée « lecture » n'écrit rien (aucun `audit(`, `.save(`, `.create(`,
   `.update(`, `.delete(`, `sealing(` dans son corps). Chaque écriture est
   **appelée** en `DEFENDED` et en `CLOSED`, et la matrice dit le résultat attendu :
   permise, ou refusée sans rien écrire (empreinte de la base inchangée, journal
   d'audit mis à part).

## Passages

| Passage | Qui | Confirmation | Effets | Journal |
|---|---|---|---|---|
| `ACTIVE → DEFENDED` | un directeur (`DECLARE_DEFENSE`) | ré-authentification < 10 min, `<details>`, date de soutenance (≤ aujourd'hui, ≥ première inscription) | `defended_on` ; invitations ouvertes révoquées | `THESIS_DEFENDED` |
| `DEFENDED → ACTIVE` | un directeur | ré-authentification, `<details>`, motif dans une liste (`wrong_date`, `declared_by_mistake`, `postponed`) | `defended_on` effacé ; demande de clôture ouverte retirée | `THESIS_DEFENSE_CANCELLED` (motif) |
| demande de clôture | le directeur ou le doctorant (`REQUEST_CLOSURE`) | ré-authentification, `<details>` ; motif `defended` en `DEFENDED` (au plus tôt `defended_on` + délai minimal) ; en `ACTIVE`, abandon avec motif dans une liste (`student_withdrew`, `cifre_terminated`, `other`) | une seule demande ouverte par thèse (contrainte en base) ; expire au bout de 14 jours | `THESIS_CLOSURE_REQUESTED` (motif) |
| confirmation : `→ CLOSED` | **l'autre partie** : un directeur si le doctorant a demandé, le doctorant si un directeur a demandé | ré-authentification, `<details>` ; statut et délai revérifiés | `closed_at`, `access_until` ; invitations révoquées ; fins d'adhésion (ci-dessous) | `THESIS_CLOSED` (motif) |
| retrait d'une demande | l'une ou l'autre partie | `POST` avec CSRF, sans ré-authentification (rien ne s'ouvre) | demande retirée | `THESIS_CLOSURE_WITHDRAWN` |
| expiration | commande quotidienne | — | demande expirée | `THESIS_CLOSURE_EXPIRED` |
| `SETUP → *`, `CLOSED → *`, `ACTIVE → CLOSED` sans motif d'abandon | personne | refusé | — | — |

- **Délai minimal** `DEFENDED → CLOSED` : `THESIS_CLOSE_MIN_MONTHS` (réglage),
  **3 mois par défaut, à confirmer (Q4)**.
- **`CLOSED` est irréversible** : aucun service ne le quitte (test), aucune écriture
  n'y est permise (matrice).
- Le passage prend le verrou de la ligne `Thesis` ; `audit()` est le dernier appel
  de sa transaction ; les fins d'adhésion suivent, chacune dans sa transaction.
- Aucune contrainte en base ne lie statut et dates : les tests existants placent
  une thèse en `DEFENDED` ou `CLOSED` par simple mise à jour. Les services
  maintiennent la cohérence (tests).

## Accès après `CLOSED`

- Lecture seule partout (garde de statut).
- **Doctorant** : garde son adhésion (lecture) jusqu'à M13c.
- **Directeur** : lecture jusqu'à `access_until` = date de clôture + 
  `THESIS_DIRECTOR_ACCESS_MONTHS` (réglage, **3 mois par défaut**), puis fin
  automatique.
- **Co-encadrants (LIRMM et entreprise), membres du CSI, invités, tuteur ST** : fin
  d'adhésion à la clôture. Tuteur : **à confirmer avec ST (Q-ST)**.
- Toutes ces fins passent par `accounts.services.end_membership_for_closure`
  (le corps de `end_membership`, sans droit d'acteur puisque le système agit) :
  `MEMBER_LEFT`, révocation du flux iCal, `MEMBER_REMOVED` avec la raison
  `thesis_closed` ou `access_period_ended`. Elles sont faites juste après la
  clôture, puis par la commande quotidienne `lifecycle_daily`, qui rattrape
  toute fin manquée et fait celles du directeur à `access_until`.

## Notifications

- Fil d'activité (cloche, page « Activité ») : `THESIS_DEFENDED`,
  `THESIS_DEFENSE_CANCELLED`, `THESIS_CLOSURE_REQUESTED`, `THESIS_CLOSED`, lisibles
  par tout membre qui voit la thèse (`VIEW_THESIS`). Pas de courriel d'activité
  pour ces types (`EMAIL_OF = NONE`) : le module envoie les siens.
- **Courriels du cycle de vie** (`LifecycleNotification`, `deliver_once`, un lien
  vers la page « Fin de thèse » seulement) :
  - soutenance déclarée → le doctorant ; soutenance annulée → le doctorant ;
  - clôture demandée → l'autre partie (le doctorant, ou chaque directeur).
- `thesis_open` du module (table) : `ACTIVE` ou `DEFENDED` ; `deliver_once`
  inchangé ; `concerned` (compte et adhésion actifs avec le rôle) et `still_due`
  (soutenance toujours déclarée, toujours annulée, demande toujours ouverte et non
  expirée) revérifiés à l'envoi.
- **Choix validé (2026-11-18)** : ces courriels partent tout de suite et ne
  suivent pas les préférences de M11b (rares ; une demande expire en 14 jours).

### Les notifications d'un module suivent son droit d'écriture (2026-11-18)

Une seule table, `apps/accounts/statuses.py` (`MODULE_STATUSES`), dit dans quels
statuts chaque module est ouvert. Elle est lue par les gardes d'écriture des
modules, leurs rappels, le `thesis_open` de leurs notifications et la construction
des résumés (`activity.digests`) ; aucun autre code ne nomme une liste de statuts
(contrôle statique).

| Module | Ouvert en |
|---|---|
| équipe, dossier administratif, objectifs, réunions, déplacements, flux iCal | `SETUP`, `ACTIVE` |
| documents (M2) | `SETUP`, `ACTIVE`, `DEFENDED` |
| conférences et accords ST (M6) | `SETUP`, `ACTIVE`, `DEFENDED` |
| justificatifs de M7 (`travel_receipts`, mission commencée avant la soutenance) | `SETUP`, `ACTIVE`, `DEFENDED` |
| cycle de vie | `ACTIVE`, `DEFENDED` |

En `DEFENDED`, les documents (M2 : version finale, corrections), M6 (demande
d'accord, relances, échéances, Informations) et les justificatifs de M7 gardent
courriels, rappels et résumés ; tout le reste est sauté (décision du 2026-11-18 :
M2 suit la règle comme les autres modules). Un
courriel d'activité suit le module de son objet ; un résumé se construit dès
qu'un module est ouvert, chaque ligne revérifiée par son module.

## Interface

**Bandeau** sur chaque page d'une thèse `DEFENDED` ou `CLOSED` : l'état, la date, ce
qui reste modifiable, un lien vers « Fin de thèse ». **Boutons d'écriture masqués**
là où le droit est refusé (les pages lisent `can()`, qui tient compte du statut ;
M7 masque aussi ce qui n'est pas un justificatif). Un test parcourt toutes les
pages de la thèse et n'y trouve aucun formulaire d'écriture refusé.


Page « Fin de thèse » (`/t/<thèse>/lifecycle/`, `VIEW_THESIS`) : statut, date de
soutenance, demande en cours (qui, motif, expiration), date de clôture et fin
d'accès du directeur. Formulaires selon les droits seulement, derrière des
confirmations `<details>` : « Déclarer la soutenance », « Annuler la
déclaration », « Demander la clôture », « Confirmer la clôture », « Retirer la
demande ». Un seul bouton plein par bloc. Lien depuis la page « Équipe ».

## Règles qui ne bougent pas

Refus par défaut et refus indiscernables ; second facteur récent pour toute
déclaration, demande ou confirmation ; minimisation (aucun nom de jury, aucun
contenu dans un événement ou un courriel) ; `deliver_once` avec `concerned` et
`still_due` ; aucune lecture scellée ; test des deux mondes pour le fil d'activité
(un événement du cycle de vie ne change rien pour qui ne voit pas la thèse) ;
journal en ajout seul, codes seulement.

## Critères d'acceptation

1. **Trous comblés** : en `CLOSED`, `store`, `add_version`, `change_level`,
   `grant_access`, `revoke_access` et `sealing` (M2), et `create_invitation`,
   `revoke_invitation`, `change_role`, `extend_membership`, `end_membership`,
   `add_membership` et l'acceptation d'invitation (M1) sont refusés. Pour chacun, le
   refus est **indiscernable** d'un refus de droit : même exception, même action et
   mêmes clés dans la ligne de journal que pour un rôle sans le droit sur une thèse
   `ACTIVE` ; même 403 pour les vues.
2. `can()` et `allowed_actions()` : pour chaque rôle, chaque action et chaque
   statut, le résultat égale la transcription indépendante du test (droit du rôle
   ET (lecture OU écriture permise au statut)). Le statut est relu en base (objet
   périmé : test).
3. Contrôle statique des copies de `OPEN_THESIS` ; matrice service × statut fermée ;
   chaque écriture appelée en `DEFENDED` et en `CLOSED` donne le résultat de la
   matrice, sans aucune écriture quand elle refuse.
4. `DEFENDED` : documents, conférences et accords ST, et fin d'adhésion permis ;
   justificatifs et montants réels permis seulement pour une mission commencée
   avant la soutenance ; tout le reste refusé.
5. Passages : chaque passage permis pour le bon rôle, refusé pour chaque autre rôle
   (et pour la même personne en confirmation) ; ré-authentification exigée (sauf
   retrait) ; délai minimal ; motif dans la liste ; une seule demande ouverte
   (contrainte en base) ; expiration à 14 jours ; `CLOSED` irréversible ; journal
   avec codes seulement.
6. Après `CLOSED` : fins d'adhésion immédiates (co-encadrants, CSI, invité,
   tuteur), directeur à `access_until` par la commande quotidienne, doctorant
   gardé ; flux iCal révoqués ; invitations ouvertes révoquées dès `DEFENDED`.
7. **Lecture inchangée** : pour chaque membre restant, ce qu'il lit (règle du fil,
   textes scellés, commentaires d'objectifs) est le même avant et après la
   clôture.
8. Courriels du cycle de vie : un lien seulement, `deliver_once`, sautés quand ils
   ne sont plus dus ou que le destinataire n'est plus concerné ; aucun autre
   courriel ne part en `DEFENDED` ni en `CLOSED`.
9. Vues : un test autorisé et un test refusé par rôle concerné, `POST` avec CSRF et
   `HTTP_ORIGIN`, ré-authentification.
10. Assertions existantes modifiées : listées avant d'y toucher.
11. En `DEFENDED`, l'accord ST demandé part au tuteur, sa relance part, chaque
    type de courriel de M6 part, un justificatif manquant d'une mission antérieure
    est rappelé, un document ajouté est annoncé, un résumé garde M6 ; un courriel
    de M4 ou de M5a reste `SKIPPED` ; mutants (M6 sans `DEFENDED`, M2 sans
    `DEFENDED`).
13. M3 : sur un dossier gelé, le droit est contrôlé avant le gel ; un non-membre
    ou un membre sans droit sur la pièce reçoit exactement le refus de droit d'une
    thèse ouverte (service et vue) ; seul le validateur apprend que le dossier est
    gelé ; mutant (contrôle du droit retiré).
12. Bandeau sur chaque page ; aucun formulaire d'écriture refusé (parcours).

## Modules terminés touchés

- M1 : `permissions.py` (garde de statut, trois actions), gardes de `add_membership`
  et `ensure_usable`, `end_membership_for_closure`, `revoke_open_invitations`.
- M4 et M11 : lecture des commentaires par `OBJECTIVE_COMMENT_READ`.
- M6 : écritures et notifications ouvertes en `DEFENDED`. M7 : justificatifs en
  `DEFENDED`, boutons masqués hors justificatifs.
- M3, M4, M5a, M6, M7, M11, M12 : statuts lus dans `apps/accounts/statuses.py`.
- M3 : `_run` contrôle le droit de l'opération avant le gel du dossier.
- M2 : courriels des documents ouverts en `DEFENDED`.
- M12 : `feeds.allowed` lit le statut en base (un objet périmé laissait créer un
  lien sur une thèse non ouverte ; trouvé par la matrice).
