# CLAUDE.md — Plateforme de gestion de thèse CIFRE

Lu automatiquement par Claude Code au début de chaque session. Les détails sont dans
`docs/` : lis le document concerné avant de toucher à un module.

- `docs/ARCHITECTURE.md` : arborescence, modèle de domaine, matrice des droits
- `docs/SECURITY.md` : primitives, chiffrement par enveloppe, journal d'audit, réglages prod
- `docs/ROADMAP.md` : ordre des modules et état d'avancement
- `docs/modules/MXX-*.md` : une fiche par module, avec critères d'acceptation

## Contexte

Plateforme web de pilotage d'une thèse CIFRE : doctorant salarié de
STMicroelectronics (Rousset), laboratoire LIRMM (équipe ARTHEMIS, Montpellier, en
zone à régime restrictif), école doctorale I2S (Université de Montpellier). Elle relie
le doctorant, le directeur de thèse, les co-encadrants LIRMM, le tuteur industriel ST
et les membres du comité de suivi individuel (CSI).

Elle **prépare et archive** ; elle **ne remplace pas** ADUM ni la plateforme ANRT.
Moins de 15 utilisateurs. Priorités, dans l'ordre : sécurité, exactitude, sobriété
pour les encadrants (ils lisent et valident, ils ne saisissent presque rien).

Le développeur est un cryptographe (doctorant), à l'aise en Python et en
mathématiques, moins familier de Django et du web : explique les choix propres à
Django ou au web en une phrase quand tu les introduis.

## Pile

- Python 3.12+, gestion des dépendances avec **uv** (`pyproject.toml`, `uv.lock`)
- Django 5.2 LTS, PostgreSQL 16, Redis 7, Celery
- Interface : templates Django + htmx servi localement (pas de CDN), pas de SPA
- Cryptographie : `cryptography` (pyca), `PyNaCl`, `argon2-cffi` ; MFA : `django-otp`
  et/ou `webauthn` ; plus tard `liboqs-python` (M10)
- Clés : KEK dérivées de `MASTER_KEY` par HKDF en dev ; Vault/OpenBao ou HSM en prod
- Qualité : pytest, pytest-django, factory-boy, ruff, mypy + django-stubs, bandit,
  pip-audit, pre-commit, detect-secrets

## Commandes

```bash
docker compose up -d db redis                 # services locaux
uv sync                                       # dépendances
uv run python manage.py migrate
uv run python manage.py runserver
uv run pytest                                 # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy apps
uv run bandit -r apps -ll
uv run pip-audit
uv run python manage.py check --deploy --settings=config.settings.prod
```

## Règles de sécurité non négociables

1. **Aucune primitive ni aucun protocole cryptographique maison.** Seuls
   `apps/vault/crypto.py` (et en M10 le module navigateur dédié) importent des
   bibliothèques cryptographiques, et uniquement leurs API de haut niveau. Pas de
   XOR, pas d'ECB, pas de nonce réutilisé, pas d'aléa hors `secrets` / `os.urandom`.
2. **Chiffrement symétrique = AEAD uniquement** (XChaCha20-Poly1305 ou AES-256-GCM).
   Données associées = `document_id || version || confidentiality_level || thesis_id`.
3. **Chiffrement par enveloppe** : une DEK par version de document, encapsulée par
   la KEK du niveau. Aucune DEK ni KEK dans la base en clair, les logs, l'admin
   Django, les messages d'erreur ou Git.
4. **Refus par défaut** : toute vue d'une thèse passe par
   `apps/accounts/permissions.py` (`can(user, action, obj)` et ses mixins). Jamais de
   contrôle d'accès seulement dans un template ou en JavaScript.
5. **Niveaux de confidentialité** : `PUBLIC`, `INTERNAL_LIRMM`, `INTERNAL_ST`,
   `CONFIDENTIAL`. Tout texte libre sensible ou tout fichier passe par
   `apps/vault/services.py` avec un niveau.
6. **Authentification** : identifiant = courriel, Argon2id, MFA obligatoire,
   ré-authentification (< 10 min) avant toute validation, changement de rôle,
   téléchargement `CONFIDENTIAL` ou export.
7. **Courriels sans contenu** : un lien vers la plateforme seulement. **Seule
   exception** : le point de contrôle hebdomadaire du journal d'audit, envoyé au
   seul `PHD_STUDENT`, qui contient uniquement `(seq, hash, signature, horodatage)`
   (ni titre de thèse, ni nom, ni clé publique). Voir `docs/decisions.md`.
8. **Journal d'audit** chaîné, en ajout seul, via `vault.services.audit(...)` pour
   toute action sensible (connexion, lecture `CONFIDENTIAL`, validation, droits, export).
9. **Secrets** uniquement par variables d'environnement. **Ne lis jamais `.env`** ni
   aucun fichier de `secrets/` ; utilise `.env.example` pour connaître les variables.
10. **Données fictives uniquement** tant que l'hébergement n'est pas validé par ST et
    le LIRMM : aucune trace de mesure, aucun résultat ST, aucune donnée réelle de
    personne dans les fixtures ou les tests.
11. Données externes (flux iCal, YAML, BibTeX, fichiers déposés) = non fiables :
    taille max, délai max, validation stricte, jamais de `eval`, de `pickle` ou de
    YAML non sûr.

Si une demande contredit une de ces règles, **arrête-toi et signale-le** au lieu de
la contourner.

## Conventions

- Code, identifiants, commentaires et messages de commit en anglais ; interface en
  français (`gettext`, `LANGUAGE_CODE = "fr-fr"`, `TIME_ZONE = "Europe/Paris"`).
- Une app Django par module (voir `docs/ARCHITECTURE.md`). Modèles métier hérités de
  `core.TimeStampedModel` ; clés primaires UUID pour tout objet exposé dans une URL.
- Logique métier dans `services.py`, vues fines, requêtes complexes dans des managers.
- **`apps/vault/chain_v1.py` n'est jamais modifié** (ni `formats.canonical_json`) :
  les lignes du journal ont été hachées avec ce code exact. Un nouveau format va
  dans `chain_v2.py` ; chaque ligne garde son format (`AuditEvent.chain_format`,
  haché comme `"v"`) et la vérification choisit le module d'après lui. Un test
  fige leurs empreintes : ne jamais les mettre à jour pour le faire passer.
- **Ordre des verrous** : verrou de KEK (`vault.keys`) **avant** verrou du journal.
  `audit()` est le dernier appel d'une transaction ; aucun `wrap` ni `new_version`
  après un `audit()` dans la même transaction (`LockOrderError`, garde dans
  `vault.keys._lock` ; seule la reprise d'un verrou de KEK déjà tenu, qui
  n'attend jamais, reste permise). Voir `apps/vault/locks.py`. Conséquence :
  chaque service du coffre s'appelle dans **sa propre** transaction, jamais
  plusieurs dans un même `transaction.atomic()` englobant.
- Chaque objet métier appartient à une `Thesis` ; toute requête est filtrée par thèse.
- Tests avec pytest et factory-boy ; pour chaque vue, au moins un test autorisé et un
  test refusé par rôle concerné.
- Un test qui enchaîne une opération journalisée (connexion, invitation...) puis une
  opération du coffre (`wrap`, chiffrement MFA, dépôt) doit être en
  `django_db(transaction=True)` : sinon la transaction englobante du test garde le
  verrou du journal et la garde lève `LockOrderError`, ce qui n'arrive pas en
  production.
- Un test de formulaire POST envoie `HTTP_ORIGIN` comme le ferait un navigateur
  (`Client(enforce_csrf_checks=True)`, origine du site, jeton CSRF de la page) :
  le client de test n'envoie aucun en-tête Origin, et un refus du contrôle
  d'origine de Django n'apparaîtrait qu'en vrai navigateur. Une page qui contient
  un formulaire POST ne répond jamais `Referrer-Policy: no-referrer` (le
  navigateur enverrait `Origin: null`) ; « null » n'est jamais ajouté à
  `CSRF_TRUSTED_ORIGINS`.
- Migrations relues et commitées avec le code qui les utilise.
- Commits : `<module>: <verbe à l'impératif> ...` (ex. `objectives: add weighted completion rate`).

## Méthode de travail

Un module = une conversation = un ou plusieurs commits.
**Plan → validation par le développeur → tests → code → revue → commit.**

- **Une branche par module** (`m0-socle`, `m1-comptes`, …). Les commits `wip` sont
  autorisés sur la branche ; la fusion dans `main` se fait seulement après la revue
  de fin du module, tous tests verts.
- N'écris aucun fichier tant que le plan n'est pas validé explicitement.
- Écris les tests des critères d'acceptation avant le code.
- Lance les tests après chaque étape et montre le résultat.
- Ne modifie pas un module terminé (coché dans `docs/ROADMAP.md`) sans le signaler.
- Si une décision durable est prise (convention, bibliothèque, schéma), propose de
  l'ajouter à ce fichier ou à `docs/`.

## Rôles (référence : `docs/ARCHITECTURE.md`)

`PHD_STUDENT`, `DIRECTOR`, `CO_SUPERVISOR`, `INDUSTRIAL_TUTOR`, `CSI_MEMBER`, `GUEST`.
