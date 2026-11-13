# Feuille de route

Cocher une case quand le module est terminé : tests verts, ruff et bandit propres,
critères d'acceptation de la fiche tous validés.

| Statut | Module | Fiche | Dépend de | Cible |
|---|---|---|---|---|
| [x] | M0 — Socle technique | `modules/M00-socle.md` | — | nov. 2026 |
| [x] | M1 — Comptes, rôles, invitations, MFA | `modules/M01-comptes.md` | M0 | déc. 2026 |
| [x] | M2 — Coffre : documents chiffrés et journal d'audit | `modules/M02-coffre.md` | M1 | déc. 2026 |
| [ ] | M3 — Dossier administratif | `modules/M03-dossier-admin.md` | M2 | janv. 2027 |
| [ ] | M4 — Objectifs et indicateurs | `modules/M04-objectifs.md` | M1 | janv. 2027 |
| [ ] | M5 — Réunions et présentations | `modules/M05-reunions.md` | M2, M4 | févr. 2027 |
| [ ] | M6 — Conférences et deadlines | `modules/M06-conferences.md` | M1 | févr. 2027 |
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
