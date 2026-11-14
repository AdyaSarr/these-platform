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
   `meetings_reminders` (timer systemd).
9. Carte « Réunions » du tableau de bord : prochaine réunion visible, actions en
   retard (toutes et les miennes).
10. Fiche d'un objectif : fragment « Actions liées ».
11. Réunion de type `CSI` : planifiable (date, lieu, participants internes, ordre
    du jour, documents), mais **compte rendu, décisions, actions et lu / approuvé
    non saisissables** (« disponible prochainement ») jusqu'à M5b.

## M5a — Critères d'acceptation
- [ ] Le `.ics` ne contient ni titre, ni ordre du jour, ni compte rendu, ni lien
      de visio, ni participant, même pour une réunion `PUBLIC` (test).
- [ ] Aucun texte de réunion en clair en base ; textes liés à (thèse, objet,
      emplacement, niveau).
- [ ] Un participant doit pouvoir lire le niveau de la réunion ; un changement de
      niveau qui exclurait un participant est refusé.
- [ ] Une action liée à un objectif apparaît sur la fiche de l'objectif.
- [ ] L'ordre du jour prérempli ne copie aucun élément d'un niveau lu par moins de
      rôles que celui de la réunion.
- [ ] Approuver un compte rendu et changer de niveau exigent un second facteur de
      moins de 10 minutes, après le contrôle des droits.
- [ ] Courriels : un lien seulement ; aucun envoi en double.
- [ ] Heures saisies à Paris ; heures inexistantes ou ambiguës refusées ; bornes de
      `meeting_bounds` testées.
- [ ] Le compte rendu d'une réunion `CSI` n'est pas saisissable en M5a.
- [ ] `demo_seed` contient des réunions passées et à venir.

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

### Partie de l'entretien CSI hors de la direction de thèse

**Source.** Le cahier des charges (`docs/specs/plateforme_these.tex`, tableau du
cadre réglementaire, art. 13) résume l'arrêté du 25 mai 2016 modifié par l'arrêté
du 26 août 2022 ainsi : « entretien annuel en trois temps : avancement, échange
avec le doctorant seul, échange avec la direction seule ». **Le texte de l'arrêté
n'est pas dans `docs/specs`** : la rédaction exacte de l'article 13
(Légifrance, JORFTEXT000046228965) et la pratique de l'ED I2S (charte, guide du
CSI) sont à vérifier par le développeur avant le plan de M5b. En particulier :
le co-encadrant et le tuteur industriel font-ils partie de la « direction de
thèse » pour le troisième temps ?

**Besoin.** Le compte rendu de l'échange avec le doctorant seul n'est lisible ni
par le directeur, ni par le co-encadrant, ni par le tuteur. Symétriquement,
celui de l'échange avec la direction seule ne devrait pas être lisible par le
doctorant. Aucun des quatre niveaux ne convient : `CONFIDENTIAL` ouvre au
directeur et au tuteur, `INTERNAL_LIRMM` au directeur et au co-encadrant,
`INTERNAL_ST` au tuteur, et le `CSI_MEMBER` ne lit que `PUBLIC`.

**Conception proposée : deux niveaux dédiés** (« audiences ») plutôt qu'un
emplacement à part avec une règle propre.

| Niveau (code) | Lecteurs (`LEVEL_ROLES`) | Usage |
|---|---|---|
| `CSI_STUDENT_ONLY` (5) | `PHD_STUDENT`, `CSI_MEMBER` | échange avec le doctorant seul |
| `CSI_DIRECTION_ONLY` (6) | `CSI_MEMBER`, `DIRECTOR` (+ selon la vérification ci-dessus) | échange avec la direction seule |

- Chaque niveau a **son propre domaine de KEK** (`LEVEL_TO_DOMAIN` est une table
  explicite, codes figés : on ajoute, on ne renumérote pas ; `vault_init` crée la
  version 1). Une DEK de ce compte rendu n'est jamais encapsulée par une KEK que
  le chemin de lecture du directeur utilise.
- **Refus par défaut structurel** : avec un emplacement scellé en `CONFIDENTIAL`
  plus une restriction par emplacement, oublier la restriction (un nouvel appel à
  `open_texts`, un export M8) ouvrirait le texte au directeur et au tuteur. Avec
  un niveau dédié, la seule table `LEVEL_ROLES` décide, et toute la mécanique
  existante (`can_access_level`, `RESTRICTED`, journal des lectures, rotation)
  s'applique sans exception.
- Ces niveaux sont **interdits partout ailleurs** : `CHECK` en base sur
  `SealedText` (couple emplacement / niveau : seuls les emplacements dédiés du
  compte rendu CSI, par exemple 0x0208 et 0x0209, les acceptent, et ces
  emplacements n'acceptent qu'eux) ; listes de niveaux des formulaires (objectifs,
  documents, réunions) inchangées ; `Document.confidentiality` refuse 5 et 6.
- Rédaction par le `CSI_MEMBER` (membre rapporteur) ; lecture par le doctorant
  (partie « doctorant seul ») ; un CSI expiré perd la lecture (adhésion active
  exigée), le doctorant la garde. Aucune approbation par la direction.
- Limite à écrire dans `SECURITY.md` : c'est un contrôle d'accès du serveur, pas
  un chiffrement de bout en bout. Qui administre le serveur et ses KEK peut lire
  ces textes ; M10 (chiffrement côté navigateur) est la réponse à cette menace.
- Le rapport et l'avis du CSI restent dans M8 et ADUM ; M5b ne garde que le
  compte rendu de l'entretien.
