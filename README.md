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

## Tests

L'ordre des tests est tiré au hasard à chaque exécution (`pytest-randomly`) : un
test qui dépend d'un autre finit par échouer. Pour garder la trace d'un échec
isolé (la graine et le nom du test), lancer la suite ainsi :

```bash
mkdir -p var
uv run pytest -p randomly 2>&1 | tee var/last-pytest.log   # var/ est ignoré par Git
```

La graine figure en tête du journal (`Using --randomly-seed=…`, masqué par `-q`) et
en dernière ligne avant le bilan (`pytest-randomly: replay with --randomly-seed=…`,
toujours affichée) ; rejouer le même
ordre avec `uv run pytest -p randomly --randomly-seed=<graine>`, et le seul test
fautif avec `uv run pytest <chemin>::<test> --randomly-seed=<graine>`.

Pour vérifier qu'aucun test ne dépend de l'heure réelle d'exécution, lancer la
suite comme si elle démarrait à un autre instant (`TEST_CLOCK`, avec un décalage
horaire explicite ; l'horloge continue d'avancer, les voyages dans le temps des
tests s'imbriquent dedans) :

```bash
TEST_CLOCK=2026-11-13T23:30:00+00:00 uv run pytest   # 00 h 30 le lendemain à Paris (hiver)
TEST_CLOCK=2027-07-01T00:30:00+02:00 uv run pytest   # 00 h 30 à Paris (été), 22 h 30 UTC
TEST_CLOCK=2027-06-15T12:00:00+00:00 uv run pytest   # après tous les instants figés des tests
```

## Vérifier la configuration de prod en local

`check --deploy --database default` exécute aussi les contrôles du coffre qui
ont besoin de la base (`vault.E001` : aller-retour réel de chiffrement MFA ;
`vault.E002` : une KEK par domaine). Avec les réglages de prod, `.env` n'est pas
lu (seul `dev.py` le lit) : il faut charger ses variables dans le shell, puis
remplacer celles que `prod.py` refuse en local (hôtes, URL en `https://`,
origines CSRF, serveur de courriel, chemin absolu du coffre) :

```bash
set -a; source .env; set +a
# pragma: allowlist nextline secret (fictitious SMTP credentials, never used)
EMAIL_URL='smtp+tls://fictif:fictif@smtp.example.org:587' \
DJANGO_SETTINGS_MODULE=config.settings.prod \
DJANGO_ALLOWED_HOSTS=these.example.org \
DJANGO_CSRF_TRUSTED_ORIGINS=https://these.example.org \
DJANGO_SITE_URL=https://these.example.org \
VAULT_STORAGE_DIR="$PWD/var/vault" \
uv run python manage.py check --deploy --database default --fail-level WARNING
```

Attendu : « System check identified no issues ». Les variables préfixées ne
valent que pour cette commande ; `DATABASE_URL`, `MASTER_KEY`, `REDIS_URL` et
`DJANGO_SECRET_KEY` viennent de `.env`. `prod.py` refuse une clé secrète de moins
de 50 caractères ou commençant par `change-me` : remplace celle de
`.env.example` avant. `source` exécute `.env` comme un script shell : une valeur
contenant des espaces ou des caractères spéciaux doit y être entre guillemets.

**Ferme ensuite ce terminal** : `set -a` a exporté tous les secrets de `.env`
(`MASTER_KEY`, mots de passe) dans le shell : tout programme lancé ensuite depuis
ce terminal en hériterait.

## Données

Tant que l'hébergement n'est pas validé par ST et le LIRMM, n'utilise que des
**données fictives**. Aucune trace de mesure, aucun résultat ST dans ce dépôt.
