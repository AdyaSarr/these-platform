# Feuille de route

Cocher une case quand le module est terminé : tests verts, ruff et bandit propres,
critères d'acceptation de la fiche tous validés.

| Statut | Module | Fiche | Dépend de | Cible |
|---|---|---|---|---|
| [x] | M0 — Socle technique | `modules/M00-socle.md` | — | nov. 2026 |
| [x] | M1 — Comptes, rôles, invitations, MFA | `modules/M01-comptes.md` | M0 | déc. 2026 |
| [x] | M2 — Coffre : documents chiffrés et journal d'audit | `modules/M02-coffre.md` | M1 | déc. 2026 |
| [x] | M3 — Dossier administratif | `modules/M03-dossier-admin.md` | M2 | janv. 2027 |
| [x] | M4 — Objectifs et indicateurs | `modules/M04-objectifs.md` | M1 | janv. 2027 |
| [x] | M5a — Réunions entre membres internes | `modules/M05-reunions.md` | M2, M4 | févr. 2027 |
| [ ] | M5b — Invités, CSI, partie confidentielle de l'entretien CSI | `modules/M05-reunions.md` | M5a | févr. 2027 |
| [ ] | M6 — Conférences et soumissions | `modules/M06-conferences.md` | M1 | févr. 2027 |
| | **MVP — mise en service avec les encadrants** | | M0–M6 | **mars 2027** |
| [ ] | M7 — Déplacements et temps ST / LIRMM | `modules/M07-deplacements.md` | M1 | printemps 2027 |
| [ ] | M8 — Rapports CSI et ANRT | `modules/M08-rapports.md` | M4, M5, M9 | avant mars 2028 |
| [ ] | M9 — Portfolio, formations, publications | `modules/M09-portfolio.md` | M2 | été 2027 |
| [ ] | M10 — Bout en bout et signatures post-quantiques | `modules/M10-e2e-pq.md` | M2 | 2028 |

Le déploiement (Caddy, TLS hybride, sauvegardes) se fait seulement après validation
de l'hébergement par ST et le LIRMM.

Avant toute mise en production : checklist de `docs/DEPLOYMENT.md` entièrement cochée.

## Plus tard

Idées validées, sans module ni date pour l'instant.

- **Synchronisation des échéances de conférences** (retirée de M6 le 2026-11-14) :
  flux iCal IACR, données `sec-deadlines`. Appel réseau sortant avec délai et taille
  max, validation stricte, jamais de YAML ; une saisie manuelle n'est jamais écrasée.
  À décider avec l'hébergement validé (sortie réseau du serveur).

- **Flux iCal personnel à jeton révocable** (après M5a ; retiré de M5 le
  2026-11-14) : URL secrète par utilisateur, ne contenant que type, date et lieu
  (jamais titre, ordre du jour ni compte rendu), jeton haché en base, révocation
  (ancien jeton → 404), journalisé. M5a ne propose que le `.ics` par réunion,
  téléchargé par un utilisateur connecté.

- **Chiffrement des titres des documents `CONFIDENTIAL`** (après M2) : en M2, le
  titre est en clair (tri, recherche, liste) et le formulaire de dépôt avertit de
  n'y mettre aucune information confidentielle. Chiffrer les titres
  `CONFIDENTIAL` sous la DEK de la version courante (ou une DEK de métadonnées),
  en acceptant de perdre le tri et la recherche côté serveur pour ce niveau.

- **Purge explicite d'un document** (après M2) : en M2, un document n'est jamais
  supprimé (seuls ses anciens blobs deviennent orphelins, listés par
  `vault_gc`). Une exigence de ST ou du RGPD peut imposer de détruire un
  document : commande ou action avec ré-authentification et journal, qui
  efface les lignes, les `wrapped_dek` et les blobs, et rappelle que les
  sauvegardes fixent le délai d'effacement réel (`docs/DEPLOYMENT.md`).

- **Alerte de blocage de l'étape TOTP** (après M1) : quand la limitation des
  tentatives bloque l'étape du second facteur d'un compte, lui envoyer un courriel
  sans contenu (lien vers la plateforme seulement, CLAUDE.md règle 7). Le blocage
  suppose que le mot de passe a été saisi correctement : c'est le signe probable
  d'une compromission du mot de passe. Envoi par Celery avec l'identifiant de
  l'utilisateur seulement, au plus un courriel par période de blocage.

- **Passages en `DEFENDED` et `CLOSED`** (aucun code ne les fait aujourd'hui :
  seul `SETUP → ACTIVE` existe, à la validation de la dernière pièce obligatoire
  du dossier administratif, M3).
  - *Qui* : le directeur, avec ré-authentification par second facteur et
    journalisation (`THESIS_STATUS_CHANGED`) ; jamais automatiquement.
  - *Quand* : `DEFENDED` après la soutenance (date saisie, procès-verbal déposé
    dans le coffre en `CONFIDENTIAL`) ; `CLOSED` à la fin des obligations (rapport
    ANRT final, fin du contrat CIFRE), au plus tôt après `DEFENDED`.
  - *Accès* : `DEFENDED` fige les objectifs et le dossier administratif (déjà le
    cas : `OPEN_THESIS`), arrête rappels et résumés hebdomadaires, garde la
    lecture pour l'encadrement ; `CLOSED` met fin aux appartenances actives
    (sauf le doctorant, en lecture seule), refuse tout dépôt et toute
    modification.
  - *Conservation* : durée à fixer avec ST, le LIRMM et l'ED (archivage de la
    thèse, RGPD) ; à l'échéance, purge explicite (voir plus haut) des documents
    et des textes scellés, le journal d'audit et ses points de contrôle restant
    conservés et vérifiables.

- **Vérifier en vrai les cookies `SameSite=Strict`** (avant la mise en service,
  sur l'HTTPS de recette) : un clic depuis Mail sur Safari (macOS) et sur iPhone
  doit ouvrir un **lien d'invitation** (POST « Continuer » accepté) et un **lien de
  notification** (rappel, résumé hebdomadaire : connexion puis retour sur la page
  demandée), avec les cookies CSRF et de session en `SameSite=Strict`. Le
  2026-11-14, un échec « CSRF cookie not set » dans Safari venait du réglage
  « Bloquer tous les cookies », pas de la plateforme : la piste `SameSite` n'a pas
  été démontrée, et la prod n'a pas été changée.
