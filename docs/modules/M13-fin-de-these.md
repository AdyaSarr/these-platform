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

### Fenêtre entre `can()` et la clôture (SR-13, accepté)

La clôture verrouille la ligne `Thesis` (`select_for_update`) ; les services qui
écrivent ne la verrouillent pas. Une écriture dont `can()` a lu le statut
`ACTIVE` (ou `DEFENDED`) **juste avant** la validation de la clôture peut donc
être enregistrée **juste après** : ni `can()` ni les gardes `OPEN_THESIS` ne
relisent le statut sous verrou.

- **Fenêtre** : le temps entre le `can()` d'une requête et la fin de sa
  transaction, de l'ordre de la milliseconde (un dépôt : le temps du chiffrement).
- **Effet borné** : une seule écriture, déjà autorisée au moment de son contrôle,
  par un membre qui avait le droit ; aucun droit gagné, aucune lecture ouverte.
  L'écriture est journalisée normalement, avec un `seq` supérieur à celui de
  `THESIS_CLOSED` : elle se repère dans le journal.
- **Pourquoi ne pas fermer la fenêtre** : il faudrait verrouiller la ligne
  `Thesis` dans chaque service qui écrit, ce qui sérialise toutes les écritures
  d'une thèse et ajoute un verrou à l'ordre des verrous du coffre
  (`vault.locks`), pour un gain nul en pratique (moins de 15 utilisateurs, une
  clôture par thèse).
- À reconsidérer si une écriture faite après la clôture devenait un problème
  (par exemple un export M13b qui doit être complet) : verrouiller alors la
  thèse dans les services concernés seulement.

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

# M13b — Export des données par le doctorant

Plan validé le 2026-11-24. App `apps/export`. Rapport de conception : celui de la
conversation du même jour (le « §4.1 » cité n'est pas dans le dépôt ; cette fiche
le remplace).

## Objectif

Le doctorant télécharge **ce qu'il lit déjà sur la plateforme**, en un ZIP :
documents déchiffrés, données métier en JSON, textes scellés déchiffrés, ses propres
lignes du journal d'audit et un manifeste lisible par machine. L'export n'ouvre
aucun droit : chaque partie passe par les règles existantes (`readable_by`,
`can_access_document`, `visible_to`, niveaux). Il prépare la fin de thèse et
l'exercice du droit d'accès ; il ne remplace aucun dépôt officiel.

## Qui et quand

- Action `THESIS_EXPORT`, **doctorant seul**, classée **lecture** (`READ_ACTIONS`) :
  le droit d'accès ne dépend pas du statut. Table des statuts : `"export"` = `SETUP`,
  `ACTIVE`, `DEFENDED`, `CLOSED` (jusqu'à la destruction de M13c, qui met fin à
  l'adhésion). `any_open()` ignore `"export"` : les résumés de M11b ne tournent
  jamais sur une thèse close.
- `POST` avec CSRF, ré-authentification par second facteur < 10 min
  (`RecentAuthRequiredMixin`), `ThesisPermissionRequiredMixin(THESIS_EXPORT)`.
- **Au plus un export par heure** et par (doctorant, thèse), compté en base
  (`ExportRecord`) sous le verrou de la ligne `Thesis` ; **un export interrompu
  compte**. La limite n'est examinée qu'après le droit : pour les six autres rôles,
  la réponse est exactement le refus de droit.

## Contenu

```
manifest.json                format these-export/1 ; chaque entrée : path, sha256,
                             size, kind, object_id, level ; bloc excluded (comptes)
LISEZMOI.txt                 ce que contient l'export et ce qu'il ne contient pas
these.json                   titre, dates, statut
equipe.json                  les autres : nom, rôle, organisme ; le doctorant :
                             nom, rôle, organisme et dates de ses adhésions
objectifs.json               objectifs, révisions, avancements, commentaires
reunions.json                réunions, décisions, actions, participants, documents liés
dossier-administratif.json   pièces, validations, réouvertures, échéances proposées
conferences.json             lieux, éditions, échéances, soumissions, accords ST,
                             prépublications
deplacements.json            missions, ordres, frais, justificatifs, jours de
                             présence, budgets, règles
documents.json               index des documents : titre, niveau, versions, fichier
documents/0001-<slug>-v3.pdf noms construits par nous (ci-dessous)
journal/mes-evenements.json  les lignes dont le doctorant est l'acteur
```

- **Documents** : dernière version de chaque document lisible ; option « toutes les
  versions ».
- **Textes scellés** déchiffrés, dans le JSON de leur objet.
- **Commentaires des encadrants** inclus (seulement ce que le doctorant lit déjà),
  auteur réduit à son nom et à son rôle (RGPD art. 15 ; art. 15.4 pour les droits
  d'autrui ; à confirmer avec le délégué à la protection des données, Q8).
- **Personnes** : partout, une personne est `{"nom", "role"}` ; jamais d'adresse.
- **Journal** : lignes de `actor` = le doctorant (de la thèse, ou sans thèse :
  connexions) : `seq`, `timestamp`, `action`, `object_type`, `object_id`, `hash`, et
  `details` filtré par une **liste blanche** de clés (une clé inconnue est retirée).
  Les lignes qui le concernent sans qu'il en soit l'acteur (lectures par d'autres)
  ne sont pas exportées : **question ajoutée à Q8**.

## Exclusions

Secrets MFA et codes de récupération ; empreintes de jetons (iCal, invitations) ;
condensats de mots de passe, sessions ; lignes d'audit des autres ; `ip_hash`,
`email_hmac` ; adresses et dates d'adhésion des autres membres ; DEK, `wrapped_dek`,
`kek_id`. Garde exhaustive : chaque modèle des apps du projet est soit exporté par un
sérialiseur nommé, soit dans la liste fermée et commentée `NOT_EXPORTED`.

## `INTERNAL_ST`

- `Thesis.st_confidential_until` (date, nulle par défaut), saisie par le **tuteur
  seul** (`SET_ST_CONFIDENTIALITY`, ré-authentification, `ST_CONFIDENTIALITY_SET`
  avec l'ancienne et la nouvelle date). Nulle ou future : `INTERNAL_ST` exclu.
  Passée : inclus.
- **Accord ST** (`StExportConsent`) : accordé par le **tuteur seul**
  (`GRANT_ST_EXPORT`, ré-authentification) pour **30 jours**, pour **tout le niveau**,
  révocable (sans ré-authentification : révoquer ferme un accès) ;
  `ST_EXPORT_CONSENT_GRANTED`, `ST_EXPORT_CONSENT_REVOKED`. Le doctorant en est
  averti par courriel (un lien).
- Exclusion **par objet entier** (métadonnées en clair comprises) et par
  descendance (décisions d'une réunion `INTERNAL_ST`, frais d'une mission
  `INTERNAL_ST`...). Le manifeste en donne le compte.
- Après `CLOSED`, plus d'accord possible (le tuteur n'est plus membre, écriture
  refusée par la garde de statut) : **provisoire, Q-ST**.

## Fabrication

1. **Instantané** : une transaction `REPEATABLE READ READ ONLY` (toutes les requêtes
   voient la base au même instant) lit les lignes, les versions retenues (DEK
   encapsulée, niveau, taille) et déchiffre les textes scellés (32 Mio au plus). Aucun
   verrou du coffre (`keys.unwrap` ne fait que lire), aucun `audit()`.
2. **Ouverture** : transaction courte : verrou de `Thesis`, limite horaire,
   `ExportRecord`, ligne de courriel, `audit(THESIS_EXPORTED)` en dernier.
3. **Flux** (`StreamingHttpResponse`, sans transaction) : une version à la fois
   (25 Mio au plus en mémoire), entrée du ZIP, SHA-256 au fil de l'eau ;
   `manifest.json` en dernier, puis le répertoire central.

Le coffre n'écrit rien : la règle « un service du coffre par transaction » et
l'ordre des verrous tiennent par construction. Lectures par
`vault.services.export_documents`, `export_read` et `export_texts` (pas d'audit
par lecture). Un blob ou une KEK disparus pendant le flux (`change_level`,
rotation) arrêtent l'export : `THESIS_EXPORT_INCOMPLETE` (code), ZIP tronqué sans
répertoire central, « interrompu » dans l'historique.

- **Aucun fichier en clair sur disque** : ni `tempfile`, ni Celery ; `zipfile` écrit
  dans un tampon vidé à chaque morceau (sortie non adressable : descripteurs de
  données).
- **Noms d'entrées** : `documents/{n:04d}-{slug}-v{k}.{ext}`, slug ASCII
  `[a-z0-9-]`, 60 caractères au plus, extension de **notre** type MIME ; chaque nom
  vérifie `^[a-z0-9][a-z0-9/-]*\.[a-z0-9]+$` (ni `..`, ni `/` en tête, ni `\`).
- **Plafond** : `EXPORT_MAX_BYTES` = 2 Gio (somme des tailles de l'instantané),
  refus avant l'ouverture, avec la suggestion « dernières versions seulement ».

## Traçabilité

- `THESIS_EXPORTED` **avant le premier octet** : options, comptes par niveau
  (documents, versions, textes), comptes d'exclusions, identifiant de l'accord ST et
  compte des éléments `INTERNAL_ST` inclus, identifiants (jamais de titre) des
  documents et objets `CONFIDENTIAL`. Il remplace les `DOCUMENT_READ` et
  `SEALED_TEXT_READ` de ces lectures (agrégation, `docs/SECURITY.md`).
- `THESIS_EXPORT_INCOMPLETE` sur un arrêt seulement (code fixe).
- **Courriel d'alerte immédiat** au doctorant, hors préférences, par le mécanisme des
  courriels du cycle de vie (M13a) : sa propre ligne (`ExportNotification`),
  `deliver_once`, jamais différé ni désactivable : « Un export de vos données a été
  fait le … » et un lien.
- **Historique** sur la page de l'export : date, options, complet ou interrompu.

## Fichier en clair

Avertissement sur la page (disque chiffré, FileVault, pas de courriel). Chiffrement
par phrase de passe (age) : reporté à M13b-2 (bibliothèque à flux à trouver,
aucune implémentation maison du format, règle 1).

## Critères d'acceptation

1. Refus pour les six autres rôles et un non-membre, en `GET` et en `POST` :
   identique octet pour octet au refus de droit, y compris quand la limite horaire
   du doctorant est active.
2. Permis en `SETUP`, `ACTIVE`, `DEFENDED`, `CLOSED` ; classement dans la matrice
   service × statut.
3. Sans second facteur récent : redirection, ni `ExportRecord` ni événement.
4. Limite d'un par heure (permis à 60 min 1 s) ; un export interrompu compte ; deux
   `POST` simultanés donnent un seul export.
5. Contenu exact, fichier par fichier ; dernière version par défaut, toutes en option ;
   textes déchiffrés.
6. Manifeste : chaque entrée, SHA-256 recalculés égaux, aucun titre.
7. Exclusions par parcours du ZIP : témoins `TEMOIN-FUITE-…` dans les secrets MFA, les
   jetons, le mot de passe, l'adresse d'un autre membre, une ligne d'audit d'autrui,
   `INTERNAL_ST` sans accord ; contrôle positif avec accord.
8. `INTERNAL_ST` : date et accord par le tuteur seul, ré-authentification, 30 jours,
   révocable, journalisé ; date passée = inclus ; aucun accord en `CLOSED`.
9. Noms d'entrées sûrs avec des titres hostiles ; aucun doublon.
10. Aucun fichier ouvert en écriture ni temporaire pendant l'export (crochet d'audit
    de Python).
11. Arrêt pendant le flux : `THESIS_EXPORT_INCOMPLETE`, « interrompu » ; plafond
    `EXPORT_MAX_BYTES` refusé avant l'ouverture.
12. `THESIS_EXPORTED` avant le premier octet, sans titre ; aucun `DOCUMENT_READ` ni
    `SEALED_TEXT_READ` pendant l'export.
13. Courriel : un, lien seulement, immédiat même avec des préférences en résumé
    hebdomadaire, envoyé en `CLOSED`.
14. Historique sur la page.
15. Garde exhaustive des modèles (exportés ou `NOT_EXPORTED`).
16. Mutants ajoutés à `scripts/mutants.toml`, tous tués.

## Assertions existantes modifiées (M13b)

Validées le 2026-11-24 avant d'y toucher ; toutes sont des ajouts à des listes
fermées, aucune attente existante n'est retirée :

- `apps/accounts/tests/test_permissions.py` : `EXPECTED` reçoit `THESIS_EXPORT`
  (doctorant), `SET_ST_CONFIDENTIALITY` et `GRANT_ST_EXPORT` (tuteur) ;
- `apps/accounts/tests/test_status_gate.py` : `READS` reçoit `THESIS_EXPORT`,
  `DEFENDED_WRITES` les deux actions du tuteur ;
- `tests/test_open_thesis_copies.py` : la table reçoit `"export"` ;
  `any_open() == (SETUP, ACTIVE, DEFENDED)` est inchangée (`NOT_DIGESTED`) ;
- `apps/core/tests/test_delivery.py` : `NOTIFYING_MODULES` reçoit `export` ;
- `tests/test_status_matrix.py` : classement de `export.*` et `vault.export_*`,
  recettes des deux écritures du tuteur (permises en `DEFENDED`) ;
- `apps/admin_file/tests/test_histories.py` (validé pendant le code) : la liste des
  fichiers qui nomment les historiques de M3 reçoit `export/snapshot.py` (lecture)
  et `export/coverage.py` (garde exhaustive) ; la vérification « aucune écriture
  hors services » s'applique à eux.

## Modules terminés touchés (M13b)

- M1 : `Thesis.st_confidential_until` (migration `accounts.0007`), trois actions et
  leurs droits (`permissions.py`), entrée `"export"` et `NOT_DIGESTED` dans
  `statuses.py`.
- M2 : `vault.services.export_documents`, `export_read`, `export_texts` (lectures
  sans audit par lecture), `crypto.Sha256`, cinq actions du journal ;
  `docs/SECURITY.md` (« Lectures agrégées »).
- M13a (en cours) : lien vers l'export et vers la confidentialité ST sur la page
  « Fin de thèse ».
- M3 à M7 : aucun changement ; `apps/export/snapshot.py` lit leurs modèles par
  leurs règles de lecture.
