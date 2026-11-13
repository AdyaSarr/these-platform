# Plateforme de gestion de thèse CIFRE

Application web de pilotage de la thèse CIFRE ST Rousset — LIRMM ARTHEMIS — ED I2S.
Kit de démarrage : spécifications et règles pour développer module par module avec
Claude Code dans VS Code.

## Contenu du kit

| Fichier | Rôle |
|---|---|
| `CLAUDE.md` | Lu automatiquement par Claude Code : pile, commandes, règles de sécurité, méthode |
| `docs/ARCHITECTURE.md` | Architecture, modèle de domaine, matrice des droits |
| `docs/SECURITY.md` | Primitives retenues, chiffrement par enveloppe, journal d'audit |
| `docs/ROADMAP.md` | Ordre des modules et état d'avancement |
| `docs/modules/M00…M10` | Une fiche par module, avec critères d'acceptation |
| `docs/PROMPTS.md` | Les prompts à coller dans Claude Code, module par module |
| `.claude/settings.json` | Permissions de Claude Code pour ce projet (interdit la lecture de `.env`) |

## Installation (une fois)

1. **Outils** : VS Code (1.94 ou plus récent), Git, Python 3.12+,
   [uv](https://docs.astral.sh/uv/), Docker Desktop (ou Docker Engine).
2. **Extension Claude Code** : dans VS Code, `Ctrl+Shift+X` (`Cmd+Shift+X` sur Mac),
   chercher « Claude Code », Installer. Se connecter avec son compte Claude.
3. **Ouvrir ce dossier** dans VS Code, puis le panneau Claude Code.
4. **Git** :
   ```bash
   git init
   git add .
   git commit -m "chore: project specifications and Claude Code setup"
   ```
   Utilise un dépôt **privé** (GitLab ou GitHub privé), ou mieux un dépôt interne
   ST ou LIRMM si l'un d'eux l'autorise.
5. Copier `.env.example` en `.env` et remplir les valeurs (jamais commité).
6. **Coffre** : créer le dossier des blobs chiffrés (`VAULT_STORAGE_DIR`, mode 700)
   avant le premier dépôt ; sans lui, lecture et écriture refusent de fonctionner :
   ```bash
   uv run python manage.py vault_init
   ```

## Méthode de travail

Un module = une conversation Claude Code = un ou plusieurs commits.
Pour chaque module : **plan → validation → tests → code → revue → commit**.
Suis `docs/PROMPTS.md`.

Commence par **M0**, puis M1 et M2 : ce sont les fondations de sécurité dont tous
les autres modules dépendent.

## Données

Tant que l'hébergement n'est pas validé par ST et le LIRMM, n'utilise que des
**données fictives**. Aucune trace de mesure, aucun résultat ST dans ce dépôt.
