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

## M9b — Publications, enseignement, diffusion, compétences (à détailler)

- **Types de lieu M6** : poster, séminaire, exposé invité, journée des doctorants,
  autre ; même circuit d'accord ST (`st_work` vrai par défaut, levé par le tuteur
  seul, second facteur, journal). M6 est terminé : lister les assertions existantes
  touchées (`VenueKind`, formulaire des revues, importateur JSON, tests de
  `apps/conferences/tests/`, `demo_seed`) **avant** de les modifier.
- `PublicationDetails(submission OneToOne)` : catégorie ADUM (ED7), DOI, identifiant
  HAL, volume et pages, clé BibTeX (scellés au niveau de la soumission, comme l'URL
  de prépublication) ; aucun niveau propre ; lecture par
  `Submission.objects.readable_by` et `sees(SUBMISSION_READ)`. Statut
  `portfolio_publications` sur `_AND_DEFENDED`. Export BibTeX seul.
- `Teaching`, `Outreach` : niveau, heures, intitulés scellés.
- `SkillNote`, `CareerNote` : **doctorant seul**.
- Récapitulatif ADUM étendu à ces sections ; le CSI n'en voit rien.
