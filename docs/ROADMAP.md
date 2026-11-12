# Feuille de route

Cocher une case quand le module est terminé : tests verts, ruff et bandit propres,
critères d'acceptation de la fiche tous validés.

| Statut | Module | Fiche | Dépend de | Cible |
|---|---|---|---|---|
| [x] | M0 — Socle technique | `modules/M00-socle.md` | — | nov. 2026 |
| [ ] | M1 — Comptes, rôles, invitations, MFA | `modules/M01-comptes.md` | M0 | déc. 2026 |
| [ ] | M2 — Coffre : documents chiffrés et journal d'audit | `modules/M02-coffre.md` | M1 | déc. 2026 |
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
