# M5 — Réunions et présentations

## Objectif
Planifier les points, comités et présentations ; garder ordres du jour, comptes
rendus, décisions et actions.

M5 est livré en **deux étapes, fusionnées séparément** (décision du 2026-11-14) :

- **M5a** (`m5-reunions`) : réunions entre **membres internes** (doctorant,
  directeur, co-encadrant, tuteur). Aucun changement de `permissions.py` au-delà
  des nouvelles actions, aucun accès par objet, aucun changement du coffre.
- **M5b** (branche suivante) : invités (`GUEST`) et membres du CSI
  (`CSI_MEMBER`) conviés à une réunion, partie confidentielle de l'entretien CSI.

Écarts à la première version de la fiche : pas de modèle `Presentation` (une
présentation est un type de réunion ; les présentations en conférence relèvent de
M6) ; compte rendu en texte scellé (`SealedText`, 16 Ko d'UTF-8 au plus : au-delà,
joindre un document du coffre) au lieu d'un `Document` ; pas de `decided_by` sur
une décision (c'est la réunion qui décide) ; durée au lieu d'une heure de fin ;
**flux iCal personnel à jeton reporté** (« Plus tard », `docs/ROADMAP.md`) ;
récurrence remplacée par un bouton « dupliquer ».

## M5a — Modèles (`apps/meetings/`)

Tous héritent de `core.TimeStampedModel`, avec une clé primaire UUID et une FK
`thesis`. Rien n'est supprimé : une réunion s'annule, une action s'abandonne, une
décision se retire.

- `Meeting(thesis, kind, presentation_venue, starts_at, duration_minutes,
  location, level, status, cancelled_at, minutes_version, minutes_updated_at,
  minutes_updated_by, ics_sequence)`
  - `kind` : `ST_TUTOR` (point tuteur ST), `SUPERVISION` (directeur /
    co-encadrant), `TRIPARTITE`, `CSI`, `PRESENTATION`, `OTHER`.
  - `presentation_venue` : `TEAM_SEMINAR`, `ST_MEETING`, `OTHER` ; renseigné si
    et seulement si `kind = PRESENTATION` (`CHECK`).
  - `starts_at` en UTC ; saisie en heure de Paris. Heure **inexistante** (passage
    à l'heure d'été) ou **ambiguë** (retour à l'heure d'hiver) refusée.
    `duration_minutes` entre 5 et 600 (`CHECK`).
  - Bornes : de `cifre_start_date − 3 mois` à `planned_end_date + 12 mois`
    (dates de Paris, une seule fonction `meeting_bounds`).
  - `location` : `ROUSSET`, `MONTPELLIER`, `VISIO`, `OTHER`.
  - `level` : les quatre niveaux ; `status` : `SCHEDULED`, `CANCELLED`
    (`cancelled_at` renseigné si et seulement si annulée, `CHECK`).
- `Attendee(meeting, membership)` — `UNIQUE(meeting, membership)` ; adhésion
  active, même thèse, rôle interne (doctorant, directeur, co-encadrant, tuteur)
  **qui peut lire le niveau de la réunion**. Un changement de niveau incompatible
  avec un participant est refusé.
- `MinutesAcknowledgement(meeting, user, minutes_version, kind: READ|APPROVED)` —
  en ajout seul, `UNIQUE(meeting, user, minutes_version, kind)` ; une marque ne
  vaut que pour la version qu'elle cite.
- `Decision(meeting, thesis, withdrawn_at)` — texte scellé.
- `ActionItem(meeting, thesis, assignee, due_date, status: OPEN|DONE|DROPPED,
  done_at, objective)` — titre scellé ; `done_at` renseigné si et seulement si
  `DONE` ; objectif facultatif de la même thèse (`PROTECT`). « En retard » =
  `OPEN` et `due_date` antérieure à la date du jour à Paris.
- `MeetingDocument(meeting, document)` — `UNIQUE(meeting, document)`, même thèse ;
  lier exige de pouvoir lire le document ; l'affichage passe par
  `can_access_document`. Le lien ne change jamais l'accès au document.
- `MeetingNotification(meeting, kind, event_id, recipient, status, attempts)` —
  `UNIQUE(kind, event_id, recipient)`, même mécanisme qu'`ItemNotification` (M3).

## M5a — Textes scellés (plage 0x02xx)

| Code | Emplacement |
|---|---|
| 0x0201 | `meetings.Meeting.title` |
| 0x0202 | `meetings.Meeting.agenda` |
| 0x0203 | `meetings.Meeting.minutes` |
| 0x0204 | `meetings.Meeting.location_detail` |
| 0x0205 | `meetings.Meeting.visio_link` |
| 0x0206 | `meetings.Decision.text` |
| 0x0207 | `meetings.ActionItem.title` |

Tous au niveau de leur réunion ; un changement de niveau rescelle tout dans une
seule transaction.

## M5a — Droits

| Action | Doctorant | Directeur | Co-encadrant | Tuteur | CSI | Invité |
|---|---|---|---|---|---|---|
| `MEETING_READ` | ✓ | ✓ | ✓ | ✓ | — (M5b) | — (M5b) |
| `MEETING_WRITE` | ✓ | ✓ | ✓ | ✓ | — | — |
| `MEETING_ACKNOWLEDGE` (nouvelle) | ✓ | ✓ | ✓ | ✓ | — (M5b) | — (M5b) |

Règles par objet, dans les services :
- tout titulaire de `MEETING_READ` voit chaque réunion (date, type, lieu) ; ses
  textes s'il peut lire le niveau, « contenu restreint » sinon (comme M4) ;
- modifier une réunion, son ordre du jour, son compte rendu, ses décisions, ses
  actions et ses documents : `MEETING_WRITE` **et** (créateur ou participant)
  **et** niveau lisible ;
- lu / approuvé : participants seulement ; clore une action : son assigné ou un
  rédacteur autorisé.

Second facteur de moins de 10 minutes, demandé après le contrôle des droits (403
d'abord) : **changer le niveau** d'une réunion ; **approuver** un compte rendu
(une validation, règle 6). Pas pour « lu », créer, modifier, annuler, dupliquer,
lier un document ni télécharger le `.ics`.

## M5a — Fonctionnalités
1. Création, modification, annulation d'une réunion ; participants pris parmi les
   membres internes actifs de la thèse.
2. Ordre du jour avant, compte rendu après (contrôle de version : 409 si le
   compte rendu a changé entre-temps), décisions et actions (responsable,
   échéance, objectif M4 facultatif), documents du coffre liés.
3. **Ordre du jour prérempli** côté serveur, ajouté à l'ordre du jour et scellé au
   niveau de la réunion : actions ouvertes et objectifs en retard, chacun
   seulement si `LEVEL_ROLES[réunion] ⊆ LEVEL_ROLES[élément]` (aucun titre ne
   descend vers un niveau lu par plus de rôles) et si l'auteur peut le lire.
4. Lu / approuvé du compte rendu par les participants ; une modification du
   compte rendu affiche les marques précédentes comme portant sur une version
   antérieure.
5. **Dupliquer** une réunion à une date choisie : copie type, sous-type, lieu,
   durée, niveau, participants et titre ; jamais l'ordre du jour, le compte
   rendu, les décisions, les actions, les documents, le détail du lieu ni le lien
   de visio.
6. **Registre des décisions** filtrable (type de réunion, période, retirées).
7. Export `.ics` par réunion, pour un utilisateur connecté : `SUMMARY` = libellé
   du type, `LOCATION` = libellé du lieu, `DESCRIPTION` = lien vers la réunion ;
   heures en UTC, `SEQUENCE`, `STATUS:CANCELLED` ; ni titre, ni ordre du jour, ni
   compte rendu, ni lien de visio, ni participants.
8. Courriels (un lien seulement, règle 7) : convocation, changement de date ou de
   lieu, annulation, compte rendu à lire, rappel la veille (date de Paris),
   action en retard (J+1, à l'assigné, une fois). Commande quotidienne
   `meetings_reminders` (timer systemd). **Revérifiés à l'envoi**
   (`core.delivery`) : thèse ouverte ; membre interne actif qui lit le niveau de
   la réunion, encore participant (ou l'assigné, pour une action) ; réunion non
   annulée ; rappel du même début, avant celui-ci ; action encore ouverte, de même
   échéance et même assigné. Une annulation reste due. Sinon `SKIPPED`.
9. Carte « Réunions » du tableau de bord : prochaine réunion visible, actions en
   retard (toutes et les miennes).
10. Fiche d'un objectif : fragment « Actions liées ».
11. Réunion de type `CSI` : planifiable (date, lieu, participants internes, ordre
    du jour, documents), mais **compte rendu, décisions, actions et lu / approuvé
    non saisissables** (« disponible prochainement ») jusqu'à M5b.

## M5a — Critères d'acceptation
- [x] Le `.ics` ne contient ni titre, ni ordre du jour, ni compte rendu, ni lien
      de visio, ni participant, même pour une réunion `PUBLIC` (test).
- [x] Aucun texte de réunion en clair en base ; textes liés à (thèse, objet,
      emplacement, niveau).
- [x] Un participant doit pouvoir lire le niveau de la réunion ; un changement de
      niveau qui exclurait un participant est refusé.
- [x] Une action liée à un objectif apparaît sur la fiche de l'objectif.
- [x] L'ordre du jour prérempli ne copie aucun élément d'un niveau lu par moins de
      rôles que celui de la réunion.
- [x] Approuver un compte rendu et changer de niveau exigent un second facteur de
      moins de 10 minutes, après le contrôle des droits.
- [x] Courriels : un lien seulement ; aucun envoi en double.
- [x] Heures saisies à Paris ; heures inexistantes ou ambiguës refusées ; bornes de
      `meeting_bounds` testées.
- [x] Le compte rendu d'une réunion `CSI` n'est pas saisissable en M5a.
- [x] `demo_seed` contient des réunions passées et à venir.

Ajoutés à la revue de fin de M5a :
- [x] Un participant dont l'adhésion est terminée (retrait, fin de CSI,
      désactivation) ne voit plus la réunion ni ses textes, ne marque plus le
      compte rendu et ne reçoit plus aucun courriel, même déjà en file.
- [x] Un identifiant de décision, d'action ou de lien d'une autre réunion est un
      refus journalisé ; le `.ics` est servi comme un téléchargement du coffre
      (`sandbox`, `no-referrer`, `nosniff`, `no-store`).

## M5b — Invités, CSI et partie confidentielle de l'entretien (à planifier)

Hors de M5a, rien n'est codé. À reprendre au plan de M5b :

- `permissions.register_object_grant(action, model, predicate)` : un prédicat qui
  ne peut qu'**accorder** une action sur un objet précis (symétrique de
  `register_document_restriction`) ; `can_on(user, action, obj)`.
- `vault.open_texts(..., scope=meeting)` / `sealing(..., scope=meeting)` :
  autorisation par `can_on`, refus journalisé de tout élément qui n'appartient pas
  à la réunion.
- Un `GUEST` ou un `CSI_MEMBER` ne voit que les réunions `PUBLIC` où il est
  convié, tant que son adhésion est active ; le convier exige
  `MEETING_ATTENDEE_GRANT` (doctorant, directeur) et un second facteur récent.
- Critère repris de la première fiche : un `GUEST` ne voit que la réunion à
  laquelle il est convié.

### Entretien du CSI : trois étapes, quatre textes

**Source** : article 13 de l'arrêté du 25 mai 2016, modifié par l'arrêté du
26 août 2022 (en vigueur depuis le 31 décembre 2022), texte officiel dans
`docs/specs/arrete-2016-article-13.md`. Il dispose :

> « Les entretiens sont organisés sous la forme de trois étapes distinctes :
> présentation de l'avancement des travaux et discussions, entretien avec le
> doctorant sans la direction de thèse, entretien avec la direction de thèse sans
> le doctorant. »

et, pour l'entretien avec le doctorant, que le comité « est particulièrement
vigilant à repérer toute forme de conflit, de discrimination, de harcèlement
moral ou sexuel ou d'agissement sexiste » et « transmet un rapport de
l'entretien au directeur de l'école doctorale, au doctorant et au directeur de
thèse ». Les membres du comité « ne participent pas à la direction du travail du
doctorant ».

**Qui lit quoi** (décision du développeur, 2026-11-14, `docs/decisions.md`) :

| Texte | Lecteurs | Rédacteurs |
|---|---|---|
| Partie 1 — présentation de l'avancement et discussion | niveau normal de la réunion (règles de M5a, CSI convié compris, M5b) | comme un compte rendu de M5a |
| Partie 2 — entretien avec le doctorant sans la direction | **doctorant et membres du CSI** seulement | membres du CSI |
| Partie 3 — entretien avec la direction sans le doctorant | **membres du CSI, directeur et co-encadrant** ; ni le doctorant, ni le tuteur | membres du CSI |
| Rapport final du CSI | **doctorant et directeur** | membres du CSI |

- **Tuteur ST exclu de la partie 2, par prudence** : l'arrêté ne parle que de la
  « direction de thèse », mais le tuteur représente l'employeur, et cette partie
  peut rapporter un conflit ou un harcèlement, y compris dans l'entreprise.
- **Partie 3 : le co-encadrant est compté dans la direction de thèse**, le tuteur
  non.
- **À signaler : la partie 3 est un texte que le doctorant ne peut pas lire sur
  sa propre plateforme.** Conséquences à traiter au plan de M5b :
  1. le doctorant gère l'équipe (`INVITE_MEMBER`, `REMOVE_MEMBER`) : il peut
     retirer un membre du CSI, qui perd alors la lecture, mais jamais lire le
     texte lui-même ;
  2. aucun export, rapport (M8) ni sauvegarde lisible par le doctorant ne doit
     contenir la partie 3 (test structurel sur les emplacements et niveaux
     dédiés) ;
  3. **limite de la protection** : c'est un contrôle d'accès du serveur, pas un
     chiffrement de bout en bout. Qui administre le serveur et ses KEK peut lire
     le texte. Tant que le doctorant (développeur) exploite le serveur, la
     partie 3 n'est protégée contre lui que par l'application. À écrire dans
     `SECURITY.md` et à présenter à l'ED, à ST et au LIRMM avec l'hébergement ;
     M10 (chiffrement côté navigateur, clés des seuls lecteurs) est la vraie
     réponse.
- **Rapport final** : l'arrêté le fait transmettre au directeur de l'école
  doctorale, au doctorant et au directeur de thèse. La plateforme le garde pour
  le doctorant et le directeur ; l'envoi à l'ED se fait **hors plateforme**
  (ADUM). Point à confirmer au plan : les membres du CSI, qui le rédigent, le
  lisent tant que leur adhésion est active. Ce rapport du comité est distinct du
  rapport d'avancement que le doctorant prépare pour le CSI (M8).

**Conception proposée : des niveaux dédiés** (« audiences ») plutôt que des
emplacements à part avec une règle propre.

| Niveau (code proposé) | `LEVEL_ROLES` | Emplacement (proposé) |
|---|---|---|
| `CSI_STUDENT_INTERVIEW` (5) | `PHD_STUDENT`, `CSI_MEMBER` | `meetings.Meeting.csi_student_interview` (0x0208) |
| `CSI_DIRECTION_INTERVIEW` (6) | `CSI_MEMBER`, `DIRECTOR`, `CO_SUPERVISOR` | `meetings.Meeting.csi_direction_interview` (0x0209) |
| `CSI_REPORT` (7) | `PHD_STUDENT`, `DIRECTOR` (+ `CSI_MEMBER` si confirmé) | `meetings.Meeting.csi_report` (0x020A) |

- **Un domaine de KEK par niveau** (`LEVEL_TO_DOMAIN` est une table explicite,
  codes figés : on ajoute, on ne renumérote pas ; `vault_init` crée la
  version 1). Aucune DEK de ces textes n'est encapsulée par une KEK que le
  chemin de lecture d'un autre rôle utilise.
- **Refus par défaut structurel** : avec un emplacement scellé en
  `CONFIDENTIAL` plus une restriction par emplacement, oublier la restriction (un
  nouvel appel à `open_texts`, un export M8) ouvrirait la partie 2 au directeur
  et au tuteur. Avec un niveau dédié, la seule table `LEVEL_ROLES` décide, et
  toute la mécanique existante (`can_access_level`, `RESTRICTED`, journal des
  lectures, rotation) s'applique sans exception.
- **Niveaux interdits partout ailleurs** : `CHECK` en base sur `SealedText`
  (chacun de ces trois emplacements n'accepte que son niveau, et ces niveaux
  n'apparaissent dans aucun autre emplacement) ; listes de niveaux des
  formulaires (objectifs, documents, réunions) inchangées ;
  `Document.confidentiality` refuse 5, 6 et 7 ; un changement de niveau de la
  réunion ne rescelle jamais ces trois textes.
- Un membre du CSI expiré perd la lecture (adhésion active exigée). Aucun
  « lu / approuvé » sur les parties 2 et 3.
- Le rapport et l'avis du CSI restent déposés dans ADUM ; la plateforme prépare
  et archive.
