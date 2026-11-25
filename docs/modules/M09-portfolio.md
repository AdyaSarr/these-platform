# M9 — Portfolio, formations, publications

## Objectif
Tenir au fil de la thèse ce que l'école doctorale et ADUM demandent chaque année
(formations suivies et heures, publications et communications, enseignement,
diffusion scientifique, compétences) et le reprendre facilement à l'inscription
annuelle. **La plateforme prépare, ADUM enregistre** : aucune donnée n'est envoyée à
ADUM, aucune formation n'est « validée » par la plateforme.

Plan validé le 2026-11-25 (`docs/decisions.md`). Deux étapes fusionnées séparément,
chacune par pull request :
- **M9a** : formations, récapitulatif ADUM, rappel avant la réinscription ;
- **M9b** : détails de publication des soumissions M6 et types de lieu M6
  manquants, enseignement, diffusion, compétences et projet professionnel.

Écarts à la première version de la fiche :
- **Aucun objectif de « 100 h »** ni d'alerte « intégrité avant la 3e année » : ni
  l'un ni l'autre n'a de source archivée (`docs/specs/questions-ed-i2s.md`, ED1 et
  ED4). Les totaux par catégorie sont affichés sans cible.
- `validated_in_adum` devient `adum_status` (`TO_ENTER`, `ENTERED`) : la plateforme
  sait seulement que le doctorant a reporté la formation dans ADUM.
- **Import BibTeX reporté** (il faudrait une dépendance ou un analyseur maison de
  données externes) ; M9b n'a qu'un export BibTeX.
- **PDF « dossier de soutenance » : M8**, qui a la chaîne LaTeX isolée.
- **Plus de modèle `Publication` ni `Communication`** : toute publication ou
  communication est une soumission M6, avec son circuit d'accord ST (M9b).
- `CareerNote` « niveau `INTERNAL` » (niveau inexistant) devient une note lisible
  par le doctorant seul (M9b).

## Sources

Seul l'article 13 de l'arrêté du 25 mai 2016 modifié est archivé
(`docs/specs/arrete-2016-article-13.md`) : le CSI « évalue les conditions de sa
formation ». Les articles 3, 12 et 15, la charte I2S, le format du portfolio ADUM
et le calendrier de réinscription sont des **questions ouvertes**
(`docs/specs/questions-ed-i2s.md`, ED1 à ED10). Chaque comportement provisoire est
noté « à confirmer (EDn) » dans l'interface ou ci-dessous.

## M9a — Formations

### Modèles (`apps/portfolio/`)

Tous héritent de `core.TimeStampedModel`, clé primaire UUID, FK `thesis`. Rien n'est
supprimé (une formation saisie par erreur se corrige ; la suppression viendra avec la
« purge explicite » de la feuille de route).

- `Training(thesis, category, hours, start_date, end_date, level, certificate,
  adum_status)`
  - `category` : `ED`, `SCIENTIFIC`, `TRANSVERSAL`, `INTEGRITY`, `OTHER`, libellés
    « à confirmer (ED2) » ;
  - `hours` : décimal, demi-heures, 0,5 à 300 ;
  - `start_date <= end_date`, bornées de `cifre_start_date − 12 mois` à
    `planned_end_date + 12 mois` ; une formation dont la fin est après aujourd'hui
    (Paris) est « à venir » et **n'entre dans aucun total** ;
  - `level` : les quatre niveaux, `PUBLIC` par défaut (le CSI ne lit que `PUBLIC` ;
    une formation interne ST se met en `INTERNAL_ST`) ; changé par une vue à part,
    second facteur récent, textes rescellés ;
  - `certificate` : FK facultative vers un `vault.Document` de la même thèse, que
    le doctorant lit ;
  - `adum_status` : `TO_ENTER` (défaut), `ENTERED` ; posé par le doctorant.
- `PortfolioPolicy(thesis, reregistration_date)` : une ligne par thèse, la date de
  la prochaine réinscription saisie par le doctorant (« à confirmer (ED9) »), vide
  par défaut.
- `PortfolioNotification(thesis, kind, target_id, window, recipient, status, …)` :
  unicité par (type, cible, fenêtre, destinataire), comme les autres modules.

### Textes scellés (plage 0x06xx)

| Code | Emplacement |
|---|---|
| 0x0601 | `portfolio.Training.title` |
| 0x0602 | `portfolio.Training.provider` |
| 0x0603 | `portfolio.Training.notes` |

Au niveau de la formation ; un changement de niveau rescelle tout. **En clair** :
catégorie, heures, dates, niveau, état ADUM, lien vers l'attestation, ce qui sert à
compter, trier et filtrer. **Scellés** : intitulé, organisme et notes, qui peuvent
révéler une orientation de recherche ou le nom d'une formation interne de ST.

### Droits

| Action | Doctorant | Directeur | Co-enc. LIRMM | Tuteur | Co-enc. entreprise | CSI | Invité |
|---|---|---|---|---|---|---|---|
| `TRAINING_READ` (selon le niveau) | ✓ | ✓ | ✓ | ✓ | — | ✓ (`PUBLIC` seulement) | — |
| `TRAINING_WRITE` | ✓ | — | — | — | — | — | — |
| `PORTFOLIO_EXPORT` (récapitulatif ADUM) | ✓ | — | — | — | — | — | — |

- `TRAINING_READ` et `PORTFOLIO_EXPORT` sont des **lectures** (`READ_ACTIONS`),
  permises dans tous les statuts ; `TRAINING_WRITE` est une écriture permise en
  `SETUP` et `ACTIVE` seulement (`statuses.py` : `"portfolio": _OPEN`).
- **Règle d'existence** (`Training.objects.readable_by`, `permissions.sees`) : une
  formation d'un niveau illisible n'est ni listée, ni comptée ; sa page répond comme
  un identifiant inconnu (même 403, journal `unreadable_level`).
- **Le CSI** (Q1 du plan) voit les formations `PUBLIC` (intitulé, organisme,
  catégorie, heures, dates), les totaux par catégorie et l'état de la formation à
  l'intégrité ; **ni les attestations** (il n'a pas `DOCUMENT_READ` ; aucun signal
  qu'il en existe une), **ni les notes**, **ni l'état ADUM**.
- **Notes et état ADUM** : le doctorant seul (`TRAINING_WRITE`).
- **Attestation** : affichée seulement à qui lit ce document
  (`can_access_document`) ; sinon aucun signal.
- **Totaux** : calculés sur les formations terminées que le lecteur lit. Pour un
  lecteur qui ne lit pas tous les niveaux (fonction de ses rôles seulement, jamais
  des formations), la mention « Calculé sur les formations que vous pouvez lire ».
- **Intégrité** : « faite » s'il existe une formation terminée, lisible, de
  catégorie `INTEGRITY`.

### Récapitulatif ADUM

- Page `adum/` (doctorant, `PORTFOLIO_EXPORT`), par **année universitaire**
  (`ThesisYear.academic_start` à `academic_end`, choix `?year=n`) : les formations
  terminées dont la fin tombe dans l'année, avec intitulé, organisme, catégorie,
  heures, début et fin (ordre et colonnes « à confirmer (ED6) »), texte
  sélectionnable pour le copier dans ADUM.
- **Par défaut, seulement les formations `PUBLIC`** : saisir dans ADUM, c'est
  divulguer hors de la plateforme. Case « Inclure les formations internes » : elles
  sont alors marquées « niveau interne : à vérifier avant saisie ».
- **CSV** (`POST adum/csv/`) : second facteur de moins de 10 minutes (règle 6,
  export), journal `PORTFOLIO_EXPORTED` (année, nombre de lignes, internes incluses
  ou non : des nombres seulement). Module `csv` de la bibliothèque standard,
  séparateur `;`, UTF-8 avec BOM, fin de ligne CRLF, colonnes fixes, heures avec
  une virgule décimale. **Toute cellule qui commence par `=`, `+`, `-`, `@`, une
  tabulation ou un retour chariot est préfixée d'une apostrophe** (un tableur
  exécuterait sinon une formule). `Content-Disposition: attachment`, nom fixe,
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.

### Rappel avant la réinscription

- Commande quotidienne `portfolio_reminders` (timer systemd, `docs/DEPLOYMENT.md`) :
  fenêtres **J-30** (8 à 30 jours avant) et **J-7** (0 à 7 jours), en dates de
  Paris, avant `PortfolioPolicy.reregistration_date`.
- Destinataire : le doctorant seul. Catégorie « Échéances » (M11b, immédiat par
  défaut). Un lien vers la page des formations seulement (règle 7).
- Envoi par `core.delivery.deliver_once` ; revérifié à l'envoi : thèse ouverte
  pour `portfolio` ; destinataire actif et `PHD_STUDENT` actif de la thèse ; même
  date de réinscription, pas encore passée. Sinon `SKIPPED`.
- **Aucun événement du fil d'activité** en M9 (Q8 du plan) : les encadrants ne
  sont prévenus de rien.

### Export M13b

`portfolio.json` : les formations (textes déchiffrés, niveau, catégorie, heures,
dates, état ADUM, identifiant de l'attestation si elle est exportée) et la date de
réinscription. Suit `c.levels` (`INTERNAL_ST` seulement avec l'accord ST).
`export/coverage.py` : `Training` et `PortfolioPolicy` exportés,
`PortfolioNotification` non (courriels).

### Critères d'acceptation (M9a)
- [x] Les totaux d'heures par catégorie sont exacts (demi-heures comprises), ne
      comptent ni une formation à venir ni une formation illisible, et portent la
      mention pour un lecteur qui ne lit pas tous les niveaux (tests).
- [x] Deux mondes sur toutes les routes GET de `portfolio`, avec contrôle positif ;
      une formation cachée répond comme un identifiant inconnu (test).
- [x] Le CSI voit les formations `PUBLIC`, leurs totaux et l'intégrité, jamais une
      attestation, une note ni l'état ADUM ; le co-encadrant entreprise et l'invité
      ne voient rien (403) (tests).
- [x] Aucun intitulé, organisme ni note en clair en base (témoin `TEMOIN-FUITE-…`).
- [x] Le CSV exige un second facteur récent, est journalisé, neutralise les
      formules et ne contient pas les formations internes par défaut (tests).
- [x] Rappel J-30 et J-7 au doctorant seul, une fois ; `SKIPPED` si la date a
      changé, si la thèse est soutenue ou si le destinataire n'est plus doctorant
      (tests).
- [x] Écritures refusées en `DEFENDED` et `CLOSED`, lectures permises (matrice des
      statuts).
- [x] `portfolio.json` dans l'export M13b, selon les niveaux exportés (test).
- [x] Mutants de `scripts/mutants.toml` (M9a) tués.
- [x] `demo_seed` : formations fictives de plusieurs catégories et niveaux, une à
      venir, une date de réinscription.

## M9b — Publications, enseignement, diffusion, compétences

Plan validé le 2026-11-25 (`docs/decisions.md`, cinq lignes M9b), réponses Q1 à Q8.

### Dans M6 (module terminé, modifié et signalé)

- **Types de lieu** : `SEMINAR`, `PHD_DAY`, `OTHER` ajoutés ; leur édition est
  **facultative** (`Venue.needs_edition` : conférence, atelier ; `has_editions` :
  tout sauf la revue). Avec une édition : échéances, rappels, date limite de demande
  d'accord et flux iCal comme une conférence ; sans édition : comme une revue (pas de
  `request_due`, aucun rappel d'échéance, aucun événement iCal).
- **Forme** : `Submission.presentation_kind` en clair (`PAPER`, `POSTER`, `TALK`,
  `INVITED_TALK`, `PAPER` par défaut), modifiable même quand la cible est verrouillée,
  reprise par « soumettre ailleurs ». **Le poster n'est pas un lieu** (Q6) : un poster à
  CHES = lieu CHES + forme `POSTER`.
- **Même circuit d'accord ST**, inchangé : `st_work` vrai par défaut, levé par le
  tuteur seul (second facteur, journal), accord `GRANTED` sur la version exacte.
- **Relance `APPROVAL_WAITING`** (Q5) : un accord attendu sans échéance de référence
  (revue, lieu sans édition, prépublication) est rappelé au tuteur à la date de Paris
  « demande + délai figé », puis chaque semaine tant qu'il est `PENDING` ; par
  `deliver_once` (concerné : tuteur actif qui lit le niveau ; encore dû : toujours
  `PENDING`, toujours sans échéance, même demande sur la même version). Catégorie
  « Échéances ».
- **Registre des textes scellés au niveau de la soumission** (`apps/conferences/sealed.py`) :
  `change_level` rescelle tout ce qui est enregistré ; M6 n'importe jamais le portfolio.
- Formulaire : la cible `venue:<id>` (lieu sans édition) remplace `journal:<id>` ;
  champ « Forme ». Page de la soumission : la forme et un lien vers ses détails.

Assertions de M6 relevées avant modification : aucune n'est contredite
(`test_a_journal_has_no_edition` des services et de l'import, le refus `edition`
d'un atelier sans édition, `test_a_journal_request_has_no_due_date`, `test_venues`,
`test_no_free_text_column_on_submissions`, la sortie de `conferences_reminders`, qui
commence toujours par le nombre de rappels d'échéance). Modifiés : le tableau des
catégories de `apps/activity/tests/test_m11b_preferences.py` (une ligne
`APPROVAL_WAITING`), `apps/conferences/tests/test_two_worlds.py` (étendu).

### Modèles (`apps/portfolio/`)

- `PublicationDetails(thesis, submission OneToOne, adum_category, presented_on)` :
  catégorie ADUM (ED7, proposée d'après le lieu et la forme : `suggested_category`) et
  date de la communication en clair ; **aucun niveau propre** (la propriété `level` lit
  celui de la soumission). Lecture : `SUBMISSION_READ` et le niveau de la soumission
  (`ReadableQuerySet.level_field = "submission__level"`, `sees`).
- `Teaching(thesis, kind, hours, start_date, end_date, level)` : types `CM`, `TD`,
  `TP`, `SUPERVISION`, `OTHER` (ED6) ; heures en demi-heures de 0,5 à 300 ; dates
  bornées comme les formations.
- `Outreach(thesis, kind, date, level)` : `SCIENCE_FESTIVAL`, `SCHOOL`,
  `PUBLIC_TALK`, `PRESS`, `ARTICLE`, `OTHER` (ED6).
- `SkillNote(thesis, category)` (ED8) et `CareerNote(thesis OneToOne)` : au niveau fixe
  `PERSONAL_LEVEL = CONFIDENTIAL` (Q1 ; `ReadableQuerySet.fixed_level`).

Rien n'est supprimé ; un texte vidé est effacé du coffre.

### Textes scellés (suite de la plage 0x06xx)

| Code | Emplacement | Niveau |
|---|---|---|
| 0x0604 à 0x0608 | `PublicationDetails.doi`, `hal_id`, `volume`, `pages`, `bibtex_key` | de la soumission |
| 0x0609 à 0x060B | `Teaching.course`, `institution`, `notes` | de l'enseignement |
| 0x060C à 0x060E | `Outreach.title`, `audience`, `notes` | de l'action |
| 0x060F, 0x0610 | `SkillNote.title`, `evidence` | `CONFIDENTIAL` |
| 0x0611 | `CareerNote.text` | `CONFIDENTIAL` |

Validation stricte des détails : DOI `10.<4 à 9 chiffres>/…` sans espace (200), HAL
`[a-z]+-<8 chiffres>(v<n>)?`, pages `12`, `12-15` ou `e12--e15` (32), volume une
ligne (32), clé BibTeX `[A-Za-z0-9:_-]{1,64}` ; aucun caractère de contrôle.

### Droits

| Action | Doctorant | Directeur | Co-enc. LIRMM | Tuteur | Co-enc. entreprise | CSI | Invité |
|---|---|---|---|---|---|---|---|
| détails : `SUBMISSION_READ` (selon le niveau de la soumission) | ✓ | ✓ | ✓ | ✓ | ✓ | — | — |
| détails : `SUBMISSION_WRITE` (`SETUP`, `ACTIVE`, `DEFENDED`) | ✓ | — | — | — | — | — | — |
| `TEACHING_READ`, `OUTREACH_READ` (selon le niveau) | ✓ | ✓ | ✓ | ✓ | ✓ | — | — |
| `TEACHING_WRITE`, `OUTREACH_WRITE` (`SETUP`, `ACTIVE`) | ✓ | — | — | — | — | — | — |
| `CAREER_NOTE_READ` (tout statut), `CAREER_NOTE_WRITE` (`SETUP`, `ACTIVE`) | ✓ | — | — | — | — | — | — |

Notes de l'enseignement et de la diffusion : le doctorant seul. Totaux d'heures et de
nombres par type, sur les éléments terminés et lisibles, avec la mention « Calculé
sur … que vous pouvez lire » pour un lecteur qui ne lit pas tous les niveaux. Les
compétences et le projet professionnel n'apparaissent nulle part ailleurs (aucun
compteur, aucun onglet visible, aucun événement du fil, ni le récapitulatif ADUM, ni
le BibTeX). La barre latérale ouvre le portfolio du co-encadrant entreprise sur
l'enseignement (`NavItem.alternatives`).

### Récapitulatif ADUM étendu et BibTeX

- Sections : formations (M9a, inchangées), **publications** (catégories
  `JOURNAL_ARTICLE`, `CONFERENCE_PAPER`), **communications** (les autres),
  **enseignement** (terminés, fin dans l'année), **diffusion** (date dans l'année,
  passée).
- Soumissions `PUBLISHED` par défaut, datées par `presented_on` ou, à défaut, la date de
  Paris de la publication ; case « Inclure les soumissions acceptées » (Q4), marquées
  « à paraître », rattachées par `presented_on` ou, sans date, à l'année en cours.
- `PUBLIC` par défaut, case pour l'interne marqué « niveau interne : à vérifier avant
  saisie » ; nombres écartés affichés (internes, acceptées).
- CSV par section (`section`, liste fermée) : mêmes règles que M9a (second facteur,
  cellules neutralisées, `;`, BOM, CRLF). Journal : `year`, `rows`, `internal` (forme
  de M9a pour les formations), plus `section` et `accepted` pour les autres sections.
- **BibTeX** (`POST adum/bibtex/`, doctorant seul, second facteur, journal
  `PORTFOLIO_EXPORTED` `section=bibtex`, nombres seulement) : toutes les soumissions
  publiées (les acceptées sur demande), `PUBLIC` par défaut ; `@article`,
  `@inproceedings` ou `@misc` selon la catégorie ; clé de l'utilisateur ou `pub-<n>`,
  suffixée en cas de doublon ; chaque caractère spécial (`\ { } $ & % # _ ~ ^`)
  remplacé par sa commande LaTeX, caractères de contrôle et de format retirés, une
  ligne par champ ; `application/x-bibtex`, nom fixe, `no-store`, `nosniff`.

### Export M13b

`portfolio.json` : `publications` (détails des soumissions exportées), `enseignements`
et `diffusion` (selon `c.levels`, `INTERNAL_ST` seulement avec l'accord ST),
`competences` et `projet_professionnel`. `coverage.py` : les cinq modèles exportés.

### Critères d'acceptation (M9b)
- [x] Nouveaux types de lieu à édition facultative, forme de la communication, même
      circuit d'accord ST pour chaque type et chaque forme (tests).
- [x] Une communication sans édition n'a ni date limite de demande, ni rappel
      d'échéance, ni événement iCal ; avec une édition, comme une conférence (tests).
- [x] Relance `APPROVAL_WAITING` à demande + délai puis chaque semaine, une fois par
      semaine, `SKIPPED` si décidée ou retirée, un lien seulement (tests).
- [x] Changer le niveau d'une soumission rescelle ses détails ; tout modèle scellé au
      niveau d'une soumission est enregistré ; M6 n'importe pas le portfolio (tests).
- [x] Détails : lus comme la soumission, écrits par le doctorant, permis en `DEFENDED`,
      refusés en `CLOSED`, validation stricte (tests).
- [x] Enseignement et diffusion : lecture par niveau pour l'encadrement, co-encadrant
      entreprise compris ; refus au CSI et à l'invité ; notes au doctorant (tests).
- [x] Compétences et projet professionnel : le doctorant seul, nulle part ailleurs
      (tests).
- [x] Aucun texte en clair en base (témoins `TEMOIN-FUITE-…`) ; aucun événement du fil.
- [x] Récapitulatif ADUM étendu (`PUBLIC` et `PUBLISHED` par défaut, cases, marques,
      CSV par section neutralisé, second facteur, journal en nombres) ; BibTeX avec
      champs hostiles (tests).
- [x] Deux mondes sur toutes les routes GET de `portfolio` et de `conferences`, avec
      contrôle positif ; un objet caché répond comme un identifiant inconnu (tests).
- [x] `portfolio.json` selon les niveaux exportés (tests) ; matrice des statuts.
- [x] Mutants de `scripts/mutants.toml` (M9b) tués.
- [x] `demo_seed` : séminaire, poster, détails, enseignement, diffusion, compétences,
      projet professionnel fictifs.
