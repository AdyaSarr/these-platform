# M11 — Activité et notifications

## Objectif

Prévenir chaque personne de ce qui se passe sur la thèse et qu'elle peut lire, et
nommer chaque élément avec un lien vers sa page :

- un **fil d'activité** dans l'interface (page « Activité », cloche dans l'en-tête),
  filtré par lecteur ;
- des **courriels d'information** (immédiat, résumé quotidien ou hebdomadaire, ou
  fil seulement), toujours un lien seulement (CLAUDE.md règle 7).

Retours qui fondent le module : un dépôt du doctorant (document, pièce, soumission,
mission) ne prévient personne ; une notification « 2 actions en retard » ne dit pas
de quoi il s'agit.

Remplace « Activité récente sur le tableau de bord », écartée de l'étape 8 de la
refonte (2026-11-16).

## Découpage

- **M11a (ce module)** : app `activity` (`Event`, `EventRead`,
  `NotificationPreference`, `ActivityNotification`) ; écriture des événements dans
  les services ; fil, cloche, marquage lu ; préférences pour la catégorie
  « informations » seulement ; purge à 13 mois. Deux écarts corrigés : le résumé
  hebdomadaire M4 passe par `core.delivery.deliver_once` (et entre dans
  `NOTIFYING_MODULES`) ; le lien de `ACTION_OVERDUE` (M5a) mène à l'action
  elle-même. **Les courriels existants de M3, M5a, M6 et M7 ne changent pas de
  comportement** ; la page Préférences dit que les demandes de décision et les
  rappels restent immédiats pour l'instant.
- **M11b (plus tard)** : les courriels existants passent sous les préférences ; le
  résumé M4 est fondu dans le résumé hebdomadaire.

## Modèles (`apps/activity/`)

Tous héritent de `core.TimeStampedModel`. **Clés primaires UUID aléatoires
(`uuid4`) partout**, jamais un entier séquentiel : l'écart entre deux identifiants
ne révèle rien (exigence a).

- `Event(thesis, kind, object_type, object_id, actor, created_at)` — `kind` et
  `object_type` dans des listes fermées ; **aucun titre, aucun niveau copié, aucun
  texte libre** : le nom affiché et le droit de lecture sont lus sur l'objet, au
  moment de l'affichage ou de l'envoi. Index `(thesis, created_at)`.
- `EventRead(event, user)` — `UNIQUE(event, user)` ; supprimé avec l'événement.
- `NotificationPreference(user, thesis, category, frequency)` —
  `UNIQUE(user, thesis, category)` ; `category` : `INFO` seulement en M11a ;
  `frequency` : `IMMEDIATE`, `DAILY`, `WEEKLY`, `FEED_ONLY`. Absente = valeur par
  défaut du rôle (ci-dessous).
- `ActivityNotification(thesis, recipient, kind, event, period_start, status,
  attempts, sent_at)` — la file d'envoi, comme les autres modules : `kind` =
  `EVENT` (un événement, envoi immédiat) ou `DIGEST_DAILY` / `DIGEST_WEEKLY`
  (`period_start` = date de Paris du début de la période) ; unique par
  `(kind, event, period_start, recipient)` : une exécution répétée n'envoie rien
  deux fois.

## Événements (M11a)

Écrits par le service de chaque module, **dans sa transaction et avant son
`audit()`** (insertion simple, aucune opération du coffre : l'ordre des verrous est
respecté). L'auteur de l'action ne voit jamais son propre événement.

| Module | `kind` | Écrit par | Lecteurs (`reads`, règle existante réutilisée) | Courriel M11a |
|---|---|---|---|---|
| M2 | `DOCUMENT_ADDED`, `DOCUMENT_VERSION_ADDED` | `vault.store`, `add_version` | `can_access_document` (rôle, niveau, autorisation, restrictions de module) | info |
| M2 | `DOCUMENT_GRANTED` | `grant_access` | le bénéficiaire, s'il lit le document | info |
| M2 | `DOCUMENT_LINKED` | liens réunion / soumission | lit le document **et** l'objet lié | info |
| M3 | `ITEM_SUBMITTED` | `submit` | droit de lecture de la section (`visible_items`) | action, immédiat, aux validateurs (ci-dessous) |
| M3 | `ITEM_VALIDATED`, `ITEM_REJECTED` | `approve`, `reject` | idem | `REJECTED` : action, immédiat, au doctorant (ci-dessous) ; `VALIDATED` : info |
| M3 | `ITEM_REOPENED` | `reopen` | idem | aucun (couvert par `REOPENED`) |
| M4 | `OBJECTIVE_CREATED`, `OBJECTIVE_CHANGED`, `PROGRESS_REPORTED` | services M4 | `OBJECTIVE_READ` **et** niveau lisible | info |
| M4 | `OBJECTIVE_COMMENTED` | `add_comment` | `OBJECTIVE_COMMENT` (`comments_for`) **et** niveau de l'objectif lisible | info |
| M5a | `MEETING_PLANNED`, `MEETING_CHANGED`, `MEETING_CANCELLED`, `MINUTES_PUBLISHED` | services M5a | `MEETING_READ` **et** niveau lisible | aucun (couverts par `INVITED`, `CHANGED`, `CANCELLED`, `MINUTES`) |
| M5a | `ACTION_ASSIGNED` | ajout ou réassignation d'une action | `MEETING_READ` **et** niveau de la réunion lisible | action, immédiat, à la personne assignée (ci-dessous) |
| M6 | `SUBMISSION_CREATED`, `SUBMISSION_STATUS_CHANGED` | services M6 | `SUBMISSION_READ` **et** niveau lisible | info |
| M6 | `APPROVAL_REQUESTED`, `APPROVAL_DECIDED` | `request_approval`, `decide_approval` | `SUBMISSION_READ` **et** niveau de la soumission lisible | aucun (couverts par M6) |
| M7 | `MISSION_PLANNED`, `MISSION_CHANGED`, `MISSION_CANCELLED` | services M7 | `Mission.objects.visible_to` | info |
| M7 | `ORDER_REQUESTED`, `ORDER_DECIDED` | `request_order`, `decide_order` | `visible_to` | aucun (couverts par M7) |
| Équipe | `MEMBER_JOINED`, `MEMBER_ROLE_CHANGED`, `MEMBER_LEFT` | services d'équipe | `VIEW_TEAM` | info |

**Jamais d'événement** pour les jours de présence (congés et vues `LIRMM_ONLY` /
`ST_ONLY`), les connexions, les lectures `CONFIDENTIAL`, les refus, les clés.

`reads(user, event)` : une fonction par type d'objet, qui **réutilise les règles
existantes** (colonne « Lecteurs »), sur l'état **actuel** de l'objet et du membre.
Un objet devenu illisible disparaît du fil.

**On n'est notifié que de ce qu'on peut lire** (décision du 2026-11-17, option B).
Les documents, les pièces M3 et les missions M7 cachent déjà leur existence à qui
ne les lit pas. Pour les **réunions, soumissions et objectifs**, dont les listes
montrent l'existence de tout objet avec des textes restreints, `reads()` = règle de
la liste **ET** niveau de l'objet lisible par le lecteur (`can_access_level`). C'est
l'alignement du fil sur les notifications existantes (M5a n'envoie déjà qu'aux
membres qui lisent le niveau) ; **les listes de M4, M5a et M6 ne changent pas**
(remplacé le 2026-11-18, lot B, SR-01 : les listes suivent désormais la même
règle, définie une seule fois par `readable_by`, que `reads()` et le flux iCal
utilisent aussi). Les
événements liés (action d'une réunion, accord d'une soumission, commentaire d'un
objectif, ordre d'une mission, lien d'un document) suivent la règle de leur objet
parent, en plus de la leur.

## Fil et cloche

- **Nom de chaque élément** : métadonnées en clair seulement — titre du document,
  libellé de la pièce, conférence et année d'une soumission, dates et destination
  d'une mission, type et date d'une réunion ou d'une action, catégorie et échéance
  d'un objectif, nom du membre (équipe). **Aucune lecture scellée**
  (`SEALED_TEXT_READ`), ni dans la cloche ni dans la page Activité.
- **Filtrer d'abord, couper ensuite** (règle de « À venir ») : cloche (8 derniers),
  compteur de non-lus, pages du fil.
- Cloche dans l'en-tête de **toutes les pages d'une thèse** : nombre de non-lus et
  8 derniers éléments ; nombre de requêtes plafonné quel que soit le nombre
  d'événements (exigence c). Un préfiltrage SQL éventuel donne **exactement** le
  même ensemble que `reads()`, vérifié par un test.
- Page « Activité » (barre latérale) : liste paginée, filtre par module en `<nav>`.
- **Ouvrir un élément** : `GET /t/<thesis_id>/activity/<event_id>/` — seul effet
  permis : marquer lu puis rediriger vers la page de l'objet (et l'ancre de l'action
  pour M5a). Événement inconnu, d'une autre thèse ou devenu illisible : la même
  réponse, journalisée comme aujourd'hui (refus indiscernable).
- **Tout marquer comme lu** : `POST` avec CSRF (exigence b).
- Conservation : **13 mois**, purge quotidienne (`manage.py activity_purge`) ; les
  `EventRead` et `ActivityNotification` de l'événement partent avec lui.

## Préférences (catégorie « informations »)

Par **(personne, thèse)**, page « Mes notifications », formulaire `POST` avec CSRF ;
changement journalisé (`ACTIVITY_PREFERENCE_CHANGED`, codes seulement : catégorie,
ancienne et nouvelle fréquence).

| Rôle | Défaut « informations » |
|---|---|
| `PHD_STUDENT` | résumé quotidien |
| `DIRECTOR`, `CO_SUPERVISOR`, `INDUSTRIAL_TUTOR`, `INDUSTRIAL_CO_SUPERVISOR` | résumé hebdomadaire |
| `CSI_MEMBER` | fil seulement (modifiable ; décision du 2026-11-17) |
| `GUEST` | rien (aucun événement lisible) |

**Ne peuvent jamais être en « fil seulement »** (fréquence modifiable en M11b, fixe
en M11a) : tout ce qui demande une action à la personne (accords ST, accords de
mission, validations M3, pièce refusée, action assignée) et les rappels d'échéance
qui la concernent ; l'invitation, la réinitialisation du mot de passe, le point de
contrôle du journal. Le refus est **dans le service**, testé, pas seulement dans le
formulaire. La page dit que ces courriels restent immédiats pour l'instant.

Résumés : quotidien à 07:00 (Paris), hebdomadaire le lundi à 07:00 ; un lien vers
le fil « depuis le dernier résumé » ; envoyé seulement s'il reste au moins un
élément lisible et non lu de la période (revérifié à l'envoi).

## Courriels

- Tous par `core.delivery.deliver_once` ; `concerned(row)` = membre actif de la
  thèse et `reads(recipient, event)` vrai maintenant (pour un résumé : la
  préférence est toujours ce résumé) ; `still_due(row)` = l'événement existe et
  n'est pas lu (immédiat), ou il reste un élément lisible et non lu de la période
  (résumé). `activity` entre dans `NOTIFYING_MODULES`.
- **Un lien seulement** : `/t/<id>/activity/<event_id>/` ou le fil ; sujet fixe par
  catégorie ; jamais un titre, un nombre, un niveau ni un nom.
- Le broker et les journaux ne voient que l'identifiant de la ligne.

Les trois nouveaux courriels immédiats (catégorie « action », aucun courriel
existant ne les couvre) :

| Courriel | Destinataires | `concerned` (à l'envoi) | `still_due` (à l'envoi) | Lien |
|---|---|---|---|---|
| Pièce déposée | validateurs de la pièce | validateur actif de la pièce (un de ses `validator_roles`, droit de lecture de la section), n'est pas l'auteur du dépôt | pièce toujours `SUBMITTED` dans le même cycle, pas encore approuvée par lui | la pièce |
| Pièce refusée | le doctorant | doctorant actif de la thèse | pièce toujours `REJECTED` dans le même cycle | la pièce |
| Action assignée | la personne assignée | toujours assignée, membre interne actif, lit le niveau de la réunion | action toujours ouverte | l'action (`#action-<uuid>`) |

Le cycle et l'action visés sont ceux de l'événement (référence de l'objet et numéro
de cycle dans la ligne de notification, jamais un texte).

## Écarts corrigés

- **Résumé hebdomadaire M4** : envoyé par `deliver_once` (avec `concerned` et
  `still_due` dans `objectives/notifications.py`) ; `objectives` entre dans
  `NOTIFYING_MODULES`. Comportement inchangé sinon.
- **`ACTION_OVERDUE` (M5a)** : le lien mène à l'action (`#action-<uuid>` sur la page
  de la réunion), plus seulement à la réunion.

## Modules terminés touchés

M1 (équipe), M2, M3, M4, M5a, M6, M7 : une écriture d'événement dans les services ;
M4 : envoi du résumé ; M5a : lien de `ACTION_OVERDUE` ; `ui` : cloche dans l'en-tête,
entrée « Activité » dans la barre latérale.

## Critères d'acceptation

1. Chaque événement du tableau est écrit par son service, dans sa transaction,
   avant `audit()`, sans `LockOrderError` (test en `transaction=True` qui enchaîne
   une opération journalisée et une opération du coffre).
2. **Deux mondes**, pour le co-encadrant LIRMM, le co-encadrant entreprise, le
   tuteur, le directeur et le membre du CSI : quand seuls diffèrent des objets que
   le rôle ne lit pas (leur **existence**, réunions, soumissions et objectifs
   compris), sont identiques octet pour octet la page Activité (toutes
   pages et filtres), la cloche et son compteur sur les pages parcourues, le nombre
   et les octets des courriels immédiats et des résumés, et les lignes
   `ActivityNotification` du rôle ; les identifiants exposés (UUID) ne révèlent
   aucun écart. Vérifié par mutants.
3. Coupe après le filtre : plus de 8 événements illisibles, plus récents que ceux du
   lecteur ; la cloche montre les siens.
4. Cloche : nombre de requêtes plafonné (`django_assert_max_num_queries`) avec des
   centaines d'événements, la plupart illisibles ; tout préfiltrage SQL donne le
   même ensemble que `reads()`, test de comparaison sur toutes les combinaisons
   rôle × type d'objet × niveau.
5. Aucune lecture scellée par la cloche ni par la page Activité.
6. `GET` d'un élément : marque lu et redirige, rien d'autre ; inconnu, autre thèse
   ou illisible : même réponse. « Tout marquer comme lu » et préférences : `POST`
   avec CSRF (`HTTP_ORIGIN`, CLAUDE.md).
7. Pour chaque vue (fil, ouverture, tout marquer lu, préférences) : un test
   autorisé et un test refusé par rôle, les sept rôles compris.
8. Préférences : défauts par rôle ; `FEED_ONLY` refusé par le service pour les
   catégories non désactivables ; changement journalisé avec des codes seulement.
9. Courriels : par `deliver_once`, `concerned` et `still_due` revérifiés à l'envoi
   (niveau changé, membre retiré, thèse terminée, déjà lu) ; un lien seulement ;
   témoin `TEMOIN-FUITE-…` absent des courriels, journaux, broker et du fil des
   lecteurs non autorisés.
10. Purge : les événements de plus de 13 mois, avec leurs lectures et notifications ;
    rien de plus récent.
11. Les trois nouveaux courriels immédiats : `concerned` et `still_due` de chacun
    testés (dépôt retiré ou approuvé entre-temps, nouveau cycle, validateur retiré
    ou auteur du dépôt ; pièce redéposée ; action réassignée, close, ou réunion
    passée à un niveau illisible).
12. Résumé M4 par `deliver_once` ; lien `ACTION_OVERDUE` vers l'action ; courriels
    existants de M3, M5a, M6, M7 inchangés.

---

# M11b — Courriels existants sous les préférences

Plan validé le 2026-11-17.

## Catégories et fréquences

| Catégorie | Fréquences permises | Défaut |
|---|---|---|
| Informations (`INFO`) | immédiat, quotidien, hebdomadaire, fil seulement | quotidien (doctorant), hebdomadaire (encadrants), fil seulement (CSI) |
| À faire (`ACTION`) | immédiat, quotidien, hebdomadaire | immédiat |
| Échéances (`DEADLINE`) | immédiat, quotidien | immédiat |

Refusé par `activity.preferences.set_preference` **et** par une contrainte en base
(`activity_preference_frequencies`) : « fil seulement » hors Informations,
« hebdomadaire » pour Échéances.

| Module | Courriel | Catégorie |
|---|---|---|
| M3 | `J14`, `J3`, `OVERDUE` (doctorant et validateurs) | Échéances |
| M3 | `DUE_PROPOSED` | À faire |
| M3 | `DUE_ACCEPTED`, `DUE_REFUSED`, `DUE_CHANGED` | Échéances |
| M3 | `REOPENED` | Informations |
| M5a | `INVITED`, `CHANGED`, `CANCELLED`, `REMINDER`, `ACTION_OVERDUE` | Échéances |
| M5a | `MINUTES` (compte rendu à approuver) | À faire |
| M6 | `DEADLINE`, `REQUEST_DUE`, `APPROVAL_PENDING` (relance), `APPROVAL_WAITING` (relance sans échéance, M9b) | Échéances |
| M6 | `APPROVAL_REQUESTED` | À faire |
| M6 | `APPROVAL_DECIDED`, `POLICY_CHANGED` | Informations |
| M7 | `ORDER_TO_REQUEST`, `RECEIPTS_MISSING`, `ORDER_PENDING` (relance) | Échéances |
| M7 | `ORDER_REQUESTED` | À faire |
| M7 | `ORDER_DECIDED`, `RULES_CHANGED` | Informations |
| M11a | pièce déposée, pièce refusée, action assignée | À faire |
| M4 | objectif changé ou résultat déclaré, à prendre en connaissance (directeur) | À faire |
| M4 | objectif en retard (`OBJECTIVE_OVERDUE`) : doctorant / autres lecteurs | Échéances / Informations |

## Fondre un courriel dans un résumé

- Chaque file de module (`ReminderLog`, `ItemNotification`, `MeetingNotification`,
  `ConferenceNotification`, `TravelNotification`) et `ActivityNotification`
  gagnent deux statuts : `DEFERRED` (attend un résumé) et `DIGESTED` (pris par le
  résumé `digest`, clé étrangère nullable vers `ActivityNotification`).
- **Mise en file** (`_queue` de chaque module → `activity.routing.dispatch`) :
  immédiat → envoi comme aujourd'hui ; quotidien ou hebdomadaire → `DEFERRED`,
  rien au broker ; fil seulement (Informations) → `SKIPPED`, code `not_wanted`.
- **Envoi** : `deliver_once(..., route=...)`, contrôlé après thèse ouverte,
  `concerned` et `still_due` : une ligne dont la préférence est passée à un
  résumé entre-temps devient `DEFERRED` (jamais perdue) ; passée à fil seulement :
  `SKIPPED` (`not_wanted`).
- **Résumé** (`digests.run`, chaque jour à 07:00 ; le lundi aussi
  l'hebdomadaire) : pour chaque destinataire, toutes ses lignes `DEFERRED` des
  catégories à cette fréquence, **quelle que soit leur date**, verrouillées,
  revérifiées par le `skip_reason` de leur module (`SKIPPED` sinon), puis
  `DIGESTED` ; plus les éléments du fil de la période si Informations est à cette
  fréquence. Rien → pas de résumé. À l'envoi du résumé, `still_due` : une de ses
  lignes tient encore, ou il reste un élément du fil lisible et non lu.
- **Retour à « immédiat »** : `set_preference` libère les lignes `DEFERRED` de la
  catégorie (`PENDING`, mise en file après commit), revérifiées à l'envoi.
- **Aucun doublon** : une ligne `DIGESTED` ou `DEFERRED` n'est jamais envoyée
  seule ; le résumé est unique par (thèse, type, période, destinataire).

## Page « Résumé »

`/t/<id>/activity/digest/<uuid>/` : le destinataire seulement (tout autre, ou un
identifiant inconnu : même réponse). Contenu **recalculé à l'affichage** : les
lignes du résumé qui tiennent encore et les éléments du fil de la période encore
lisibles, chacun nommé (métadonnées en clair) et lié à sa page.

## Résumé M4 du lundi

Fondu : les objectifs changés ou au résultat déclaré à prendre en connaissance
sont des événements « À faire » pour le directeur ; un objectif en retard devient
l'événement `OBJECTIVE_OVERDUE` (écrit une fois par `activity_digests`, sans
auteur). `WeeklySummaryLog` reste en lecture, ses lignes soumises à la purge de
13 mois ; la commande `objectives_weekly_summary` et son timer sont retirés. La
suppression de la table viendra plus tard, dans une migration séparée (ROADMAP).

## Critères d'acceptation (M11b)

1. Chaque courriel de chaque module a une catégorie (test exhaustif).
2. `set_preference` et la base refusent « fil seulement » hors Informations et
   « hebdomadaire » pour Échéances ; même refus par un `POST` forgé.
3. Une ligne naît `DEFERRED` en quotidien ou hebdomadaire, dans chaque module ; le
   résumé la prend une fois ; elle n'est jamais envoyée seule.
4. Aucune perte : mise en file en immédiat, préférence passée à un résumé avant
   l'envoi → `DEFERRED` puis dans le résumé ; résumé → immédiat : libérée.
5. Aucun doublon : deux `digests.run()` simultanés ; une ligne `FAILED` remise en
   file par un module n'est pas aussi dans un résumé.
6. Revérification à la construction et à l'envoi du résumé (`SKIPPED`).
7. Deux mondes étendus aux résumés qui regroupent des courriels de modules ; page
   Résumé autorisée et refusée (indiscernable), mutants.
8. **Avec les valeurs par défaut, aucun test existant de M3, M5a, M6, M7 n'est
   modifié** ; si l'un doit l'être, arrêt et liste avant d'y toucher.
   *Assoupli le 2026-11-17 (option a)* : les cinq courriels classés Informations
   (`REOPENED`, `APPROVAL_DECIDED`, `POLICY_CHANGED`, `ORDER_DECIDED`,
   `RULES_CHANGED`) suivent par défaut le résumé des Informations ; les tests qui
   vérifient leur envoi immédiat posent explicitement la préférence
   « Informations : immédiat » dans leur préparation (`_information_at_once`, et
   la démonstration à l'arrivée de l'équipe) ; **aucune assertion n'est changée**.
9. Le test sauté « objectif `INTERNAL_ST` » est remplacé par un test qui vérifie
   qu'un objectif ne peut pas avoir ce niveau (service et base).
