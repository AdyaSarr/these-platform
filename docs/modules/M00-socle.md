# M0 — Socle technique

## Objectif
Un projet Django qui démarre, sécurisé par défaut, testé, avec l'outillage qualité.

## À produire
- `pyproject.toml` (uv) : django 5.2, psycopg[binary], django-environ, argon2-cffi,
  django-csp, django-htmx, celery, redis ; dev : pytest, pytest-django, factory-boy,
  ruff, mypy, django-stubs, bandit, pip-audit, pre-commit, detect-secrets.
- `config/settings/{base,dev,prod,test}.py`, lecture de `.env` via django-environ.
- `docker-compose.yml` : PostgreSQL 16 et Redis 7 (ports locaux uniquement,
  mots de passe depuis `.env`).
- `apps/core/` : `TimeStampedModel` abstrait (`created_at`, `updated_at`,
  `created_by`), page d'accueil, vue `/health/`.
- Modèle utilisateur personnalisé `accounts.User` créé **dès M0** (courriel comme
  identifiant), même si le reste de M1 vient après.
- Gabarit de base (`templates/base.html`) : htmx chargé localement (pas de CDN),
  CSS minimal, langue `fr`.
- `.pre-commit-config.yaml`, configuration ruff, `pytest.ini` ou section pyproject.
- `config/celery.py` minimal.

## Critères d'acceptation
- [ ] `docker compose up -d db redis && uv run python manage.py migrate` fonctionne.
- [ ] `uv run pytest` passe (au moins : santé, page d'accueil exige la connexion).
- [ ] `PASSWORD_HASHERS` commence par Argon2.
- [ ] `prod.py` applique tous les paramètres de `docs/SECURITY.md` ; un test vérifie
      `DEBUG is False`, HSTS, cookies sécurisés quand on charge `prod`.
- [ ] `manage.py check --deploy --settings=config.settings.prod` sans avertissement
      (hors certificats).
- [ ] Aucun secret dans le dépôt ; `.env.example` documente chaque variable.
- [ ] `ruff`, `bandit` propres.
