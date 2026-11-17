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

La suite complète tourne en deux phases, avec la même graine (`scripts/test_all.sh`) :
d'abord en parallèle (pytest-xdist, une base `test_<nom>_gwN` par worker) tous les
tests sauf ceux marqués `serial`, puis seuls ceux marqués `serial` (threads, délais,
pytest enfants). Le code de sortie est celui de la première phase en échec :

```bash
scripts/test_all.sh > var/last-pytest.log 2>&1; echo "pytest exit=$?"   # var/ est ignoré par Git
PYTEST_WORKERS=8 PYTEST_SEED=1234 scripts/test_all.sh                   # réglages
scripts/test_all.sh apps/travel -x                                      # arguments passés aux deux phases
```

`uv run pytest` seul tourne en série (pas de `-n` par défaut), comme avant.

Les tests tournent de préférence sur leur propre serveur PostgreSQL, le service
`db-test` : même image que `db`, données en mémoire (perdues à l'arrêt du
conteneur), sans fsync. Seule la durabilité après une panne change, jamais le
comportement SQL ; la suite complète dure environ deux fois moins longtemps. Il
faut `TEST_POSTGRES_PASSWORD` et `TEST_DATABASE_URL` dans `.env` (voir
`.env.example`), puis :

```bash
docker compose up -d db-test
```

Sans `TEST_DATABASE_URL`, les tests utilisent le serveur de `DATABASE_URL`, comme
avant, et pytest le signale en fin de sortie. `fsync=off` et `tmpfs` ne figurent
que sur `db-test`, jamais sur `db`, qui garde son volume (test statique).

L'ordre des tests est tiré au hasard à chaque exécution (`pytest-randomly`) : un
test qui dépend d'un autre finit par échouer. Sous xdist, chaque worker écrit les
tests qu'il a lancés, dans l'ordre, dans `var/pytest-order/<empreinte>/gwN.txt` ;
après un échec, la fin de la sortie donne la commande qui rejoue ce worker seul :
`uv run pytest -p no:xdist --randomly-seed=<graine> --randomly-dont-reorganize @var/pytest-order/<empreinte>/gwN.txt`.

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
TEST_CLOCK=2026-11-13T23:30:00+00:00 scripts/test_all.sh   # 00 h 30 le lendemain à Paris (hiver)
TEST_CLOCK=2027-07-01T00:30:00+02:00 scripts/test_all.sh   # 00 h 30 à Paris (été), 22 h 30 UTC
TEST_CLOCK=2027-06-15T12:00:00+00:00 scripts/test_all.sh   # après tous les instants figés des tests
```

## Vérifier la configuration de prod en local

```bash
scripts/check_deploy.sh                       # check --deploy, réglages de prod
scripts/check_deploy.sh --database default    # + contrôles du coffre sur la base
```

Avec les réglages de prod, `.env` n'est pas lu (seul `dev.py` le lit). Le script
charge `.env` **dans un sous-shell** (`( set -a; . ./.env; … )`), remplace ce que
`prod.py` refuse en local par des valeurs fictives (`https://these.example.org`,
SMTP et expéditeur fictifs, origines CSRF) et prend un dossier de coffre
temporaire, supprimé à la fin. Aucune variable ne reste dans le terminal, même si
le script est sourcé par erreur. `--database default` exécute aussi `vault.E001`
(aller-retour réel de chiffrement MFA) et `vault.E002` (une KEK par domaine).

Attendu : « System check identified no issues ». `DATABASE_URL`, `MASTER_KEY`,
`REDIS_URL` et `DJANGO_SECRET_KEY` viennent de `.env`. `prod.py` refuse une clé
secrète de moins de 50 caractères ou commençant par `change-me` : remplace celle
de `.env.example` avant. `.env` est lu comme un script shell : une valeur
contenant des espaces ou des caractères spéciaux doit y être entre guillemets.

**Jamais `source .env` dans le terminal** : `set -a` y exporterait tous les secrets
de `.env`, et `DJANGO_SETTINGS_MODULE=config.settings.dev`, que pytest prendrait
au lieu des réglages de test (`scripts/test_all.sh` et `conftest.py` refusent
alors de démarrer). Toute commande qui a besoin de `.env` se lance dans un
sous-shell `( … )`.

## Données

Tant que l'hébergement n'est pas validé par ST et le LIRMM, n'utilise que des
**données fictives**. Aucune trace de mesure, aucun résultat ST dans ce dépôt.

## Démonstration sur un poste local

Une base séparée (`<base de DATABASE_URL>_demo`, coffre `var/vault-demo`) reçoit
une thèse de démonstration entièrement fictive, écrite par les services comme le
ferait un vrai usage (`apps/demo/scenario.py`). La base de dev n'est jamais touchée.

```bash
docker compose up -d db redis
scripts/demo_reset.sh            # montre ce qui sera supprimé, demande « oui »
DJANGO_SETTINGS_MODULE=config.settings.demo uv run python manage.py runserver
```

Le script supprime puis recrée la base `_demo`, `var/vault-demo` et
`var/demo-audit`, lance `migrate` puis `demo_seed`, exporte la clé publique et
un point de contrôle dans `var/demo-audit/`, puis vérifie le journal avec
`audit_verify`. À la fin, il affiche **une seule fois** un lien par personne :
chacune choisit son mot de passe, se connecte, puis enrôle son second facteur.
Ensuite, « Mot de passe oublié » fonctionne normalement. Pour recommencer, on
relance le script : `demo_seed` refuse de s'exécuter sur une base non vide.
