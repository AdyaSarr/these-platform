# M6 — Conférences et soumissions

## Objectif
Les conférences et revues visées, leurs échéances dans leur fuseau d'origine, les
soumissions de la thèse et l'**accord de publication ST** (CIFRE), avec son délai de
relecture décompté à rebours.

Écarts à la première version de la fiche (plan validé le 2026-11-14) :
- **Aucune source en ligne** : ni flux iCal IACR, ni YAML `sec-deadlines`, ni tâche de
  synchronisation (reportés dans « Plus tard », `docs/ROADMAP.md`). Saisie manuelle et
  import d'un fichier JSON local au schéma strict ; **jamais de YAML**.
- Échéances en lignes typées (`Deadline`) plutôt qu'en colonnes fixes d'`Edition`.
- Statuts de la soumission revus ; délai de relecture **par thèse**, figé dans chaque
  demande d'accord ; `core_rank`, `topics` retirés ; **budget reporté à M7**.
- Alertes J-30, J-14, J-7, J-1 (au lieu de J-7 seulement).


**Règle d'existence (lot B, SR-01, `docs/decisions.md` du 2026-11-18).** On ne voit
l'existence que de ce qu'on peut lire : un objet de ce module n'existe pour un
lecteur que s'il a l'action de lecture du module **et** lit son niveau
(`Submission.objects.readable_by`, `permissions.sees`). Un objet d'un autre niveau
n'est ni listé, ni compté, ni lié ; sa page répond comme un identifiant inconnu.
Aucun signal non plus d'un document lié illisible. Test des deux mondes :
`apps/conferences/tests/test_two_worlds.py` (méthode : `tests/two_worlds.py`).
Exception assumée et documentée : le **catalogue** (lieux, éditions, échéances)
est partagé par l'équipe ; mais les éditions « visées » de la carte du tableau de
bord et les dates de demande d'accord ne se déduisent que des soumissions
lisibles. Un accord de ST reste affiché sans le nom d'un document illisible.

## Modèles (`apps/conferences/`)

Tous héritent de `core.TimeStampedModel`, clé primaire UUID, FK `thesis` (catalogue
**par thèse**). Rien n'est supprimé.

- `Venue(thesis, kind: CONFERENCE|WORKSHOP|JOURNAL, acronym, name, url)` —
  `UNIQUE(thesis, kind, acronym)` ; URL en `https` seulement. Informations publiques,
  en clair (avertissement sur le formulaire).
- `Edition(thesis, venue, year, location, event_start, event_end)` — `UNIQUE(venue,
  year)`, `event_start <= event_end` ; une revue n'a pas d'édition.
- `Deadline(thesis, edition, kind, due_at, tz_name, cancelled_at)` — `kind` :
  `ABSTRACT`, `PAPER`, `REBUTTAL`, `NOTIFICATION`, `CAMERA_READY`, `REGISTRATION` ;
  `due_at` en UTC ; `tz_name` = `"AoE"` (UTC−12 fixe) ou un nom IANA connu
  (`Etc/GMT+12` refusé, son signe POSIX est inversé). Saisie dans le fuseau de
  l'échéance ; heure inexistante ou ambiguë refusée. Affichée en heure de Paris
  **avec** l'heure et le fuseau d'origine.
- `PublicationPolicy(thesis, st_review_days)` — une ligne par thèse, 0 à 180 jours,
  30 par défaut ; réglée par le tuteur seul.
- `Submission(thesis, venue, edition, objective, level, status, st_work,
  submitted_version, published_version, submitted_at, published_at)` — une revue sans
  édition, une conférence avec une édition de la même venue ; objectif M4 facultatif ;
  `st_work` **vrai par défaut** ; `st_work ⇒ level ∈ {PUBLIC, INTERNAL_ST,
  CONFIDENTIAL}` (le tuteur lit ce qu'il accorde).
  Statuts : `PREPARING`, `ST_REQUESTED`, `ST_GRANTED`, `SUBMITTED`, `ACCEPTED`,
  `REJECTED`, `WITHDRAWN`, `PUBLISHED` ; terminaux : `REJECTED`, `WITHDRAWN`,
  `PUBLISHED`.
- `SubmissionAuthor(submission, user)` — co-auteurs internes (membres internes actifs
  qui lisent le niveau).
- `SubmissionDocument(submission, document)` — documents du coffre (versions de
  l'article).
- `StApproval(submission, purpose, version, review_days, request_due, requested_by,
  decision, reason, decided_by, decided_at)` — `purpose` : `SUBMISSION`,
  `CAMERA_READY`, `PREPRINT` ; `version` = la `DocumentVersion` exacte relue ;
  `decision` : `PENDING`, `GRANTED`, `REFUSED`, `WITHDRAWN` ; motif de refus en liste
  fermée (`PATENT_PENDING`, `CONFIDENTIAL_INFORMATION`, `ST_RESULTS_NOT_CLEARED`,
  `CHANGES_REQUESTED`, `OTHER`), commentaire scellé facultatif ; une seule demande en
  attente par (soumission, objet) ; `decided_by <> requested_by` ; décidée = jamais
  modifiée.
- `Preprint(submission, platform: EPRINT|ARXIV|HAL|OTHER, posted_on, version)` — URL
  publique scellée.
- `ConferenceNotification(kind, target_id, window, recipient, …)` — unicité par
  (type, cible, fenêtre, destinataire) ; destinataire revérifié à l'envoi.

## Textes scellés (plage 0x03xx)

| Code | Emplacement |
|---|---|
| 0x0301 | `conferences.Submission.title` |
| 0x0302 | `conferences.Submission.abstract` |
| 0x0303 | `conferences.Submission.external_authors` |
| 0x0304 | `conferences.Submission.notes` |
| 0x0305 | `conferences.StApproval.comment` |
| 0x0306 | `conferences.Preprint.url` |

Tous au niveau de la soumission ; un changement de niveau rescelle tout. **L'URL d'une
prépublication est scellée comme le reste**, même si la page est publique : la lier à
une soumission non `PUBLIC` révélerait quel article interne elle est.

## Accord de publication ST

Règle unique : **tout acte public — soumettre, publier la version finale, prépublier —
désigne une version exacte d'un document lié et exige, si `st_work`, un accord
`GRANTED` sur cette version, pour cette soumission.** Un accord n'est jamais repris
d'une autre soumission (« soumettre ailleurs » crée une soumission neuve).

- **Demande** (doctorant) : une version d'un document lié, un objet (`SUBMISSION`,
  `CAMERA_READY`, `PREPRINT`) ; refusée sans tuteur actif ; délai figé
  (`review_days`) et date limite figée (`request_due`) = **date à Paris** de
  l'échéance de référence − délai : `PAPER` pour `SUBMISSION`, `CAMERA_READY` pour
  `CAMERA_READY`, aucune pour `PREPRINT` ni pour une revue.
- **Décision** (tuteur, `VALIDATE_PUBLICATION`, second facteur de moins de 10 minutes
  après le contrôle des droits) : `GRANTED` ou `REFUSED` (motif obligatoire). Pour
  l'objet `SUBMISSION`, la soumission passe `ST_REQUESTED` → `ST_GRANTED` ou revient
  `PREPARING`.
- **Soumettre** (`→ SUBMITTED`) : accord sur la version soumise, qui doit être encore
  la version courante de son document (sinon 409 `approval_outdated`).
- **Version finale** (`ACCEPTED → PUBLISHED`) : accord sur la version publiée ; si
  c'est la version déjà accordée, aucune nouvelle demande ; sinon demande `CAMERA_READY`
  avec sa propre date limite.
- **Prépublier** (ePrint, arXiv, HAL, autre) : possible dans tout état non terminal ;
  accord sur la version prépubliée ; enregistre la plateforme, la date (Paris, pas dans
  le futur) et l'URL publique (`https`, scellée) ; journalisé.
- `st_work` ne se désactive que par le tuteur (second facteur, journalisé). Le délai
  (`PublicationPolicy`) ne se règle que par le tuteur (second facteur, journalisé, de
  → vers) ; **le doctorant est notifié** (lien seulement), ses dates limites bougeant
  pour les demandes futures.
- Le délai vit dans M6, pas dans le dossier administratif : le contrat de
  collaboration (M3) le fixe, mais la plateforme n'interprète pas un document chiffré ;
  la page de réglage rappelle l'état de la pièce M3.

## Droits

| Action | Doctorant | Directeur | Co-encadrant | Tuteur | CSI | Invité |
|---|---|---|---|---|---|---|
| `CONFERENCE_READ` | ✓ | ✓ | ✓ | ✓ | — | — |
| `CONFERENCE_WRITE` | ✓ | ✓ | ✓ | ✓ | — | — |
| `SUBMISSION_READ` (textes selon le niveau) | ✓ | ✓ | ✓ | ✓ | — | — |
| `SUBMISSION_WRITE` | ✓ | — | — | — | — | — |
| `VALIDATE_PUBLICATION` (existe) | — | — | — | ✓ | — | — |

Second facteur récent : décider un accord, changer le niveau d'une soumission,
régler le délai, désactiver `st_work`.

## Rappels (un lien seulement, règle 7)

Commande quotidienne `conferences_reminders` (timer systemd). Fenêtres J-30, J-14,
J-7, J-1, comptées **en dates dans le fuseau de l'échéance** ; la date limite de
demande d'accord en dates de Paris. Échéances pertinentes selon le statut des
soumissions en cours ; destinataires : doctorant et co-auteurs internes ; le tuteur
pour un accord en attente (J-7, J-1 avant l'échéance de référence).

**Revérifiés à l'envoi** (`core.delivery`) : thèse ouverte ; membre interne actif
qui lit les soumissions et leur niveau, avec le rôle du type (doctorant ou
co-auteur interne, tuteur, demandeur) ; échéance du même instant, ni annulée ni
passée, encore pertinente pour une soumission en cours du destinataire ; date
limite de demande du même jour, non passée, avant toute demande ; accord encore en
attente (même échéance de référence pour le rappel). Une décision et un
changement de délai restent dus. Sinon `SKIPPED`.

## Import JSON local

`manage.py conferences_import <fichier> --thesis <id> --as <courriel du doctorant>`,
`--dry-run` par défaut (`--apply` pour écrire) : 256 Ko au plus, UTF-8 strict, clés
en double, `NaN`, flottants refusés, schéma vérifié champ par champ, profondeur et
nombres d'éléments bornés ; crée ce qui manque, **ne modifie jamais** une ligne
existante ; une transaction ; sortie : des nombres seulement ; aucun appel réseau.

## Critères d'acceptation
- [x] Une échéance AoE et une échéance IANA sont converties correctement en UTC et en
      heure de Paris, de part et d'autre des changements d'heure (tests).
- [x] Date limite de demande = date à Paris de l'échéance de référence − délai figé
      dans la demande (tests).
- [x] « Soumis », « publié » et une prépublication sont refusés sans accord `GRANTED`
      sur la version exacte (tests), y compris avec un accord sur une version dépassée.
- [x] Seul le tuteur décide un accord, désactive `st_work` et règle le délai, avec un
      second facteur récent demandé après le contrôle des droits.
- [x] Rappels J-30/J-14/J-7/J-1 calculés en dates du fuseau de l'échéance ; aucun
      envoi en double ; un lien seulement.
- [x] Un import ne modifie jamais une ligne existante ; un fichier mal formé ou trop
      gros est rejeté sans rien écrire (tests).
- [x] Aucun titre, résumé, nom d'auteur externe, commentaire ou URL de prépublication
      en clair en base.
- [x] `demo_seed` : conférences fictives, une soumission acceptée, une en attente
      d'accord ST, une refusée.

Ajoutés à la revue de fin de M6 :
- [x] La cible (conférence, édition) d'une soumission est verrouillée dès qu'un
      accord ST pour soumettre est demandé ou accordé (409 `target_locked`) :
      changer de cible, c'est « soumettre ailleurs », avec un nouvel accord.
- [x] Une demande d'accord exige un document qu'un tuteur actif peut lire ;
      l'historique des accords ne montre pas le titre d'un document illisible.

## Ajouts de M9b (2026-11-25, module terminé modifié et signalé)

Détail dans `modules/M09-portfolio.md` (M9b) et `docs/decisions.md` :
- types de lieu `SEMINAR`, `PHD_DAY`, `OTHER`, à édition facultative ; forme de la
  communication `Submission.presentation_kind` (`PAPER`, `POSTER`, `TALK`,
  `INVITED_TALK`) ; le poster n'est pas un lieu ; même circuit d'accord ST ;
- relance `APPROVAL_WAITING` au tuteur d'un accord attendu sans échéance de référence ;
- registre `apps/conferences/sealed.py` des textes scellés au niveau d'une soumission
  hors de sa table, rescellés par `change_level` ;
- la page d'une soumission montre sa forme et un lien vers ses détails de publication.
