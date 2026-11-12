# Décisions durables

Une ligne par décision : date, décision, raison. Ajouter en bas, ne pas réécrire
l'historique (une décision annulée reçoit une nouvelle ligne qui la remplace).

| Date | Décision | Raison |
|---|---|---|
| 2026-11-12 | Python **3.13** épinglé (`.python-version`, `requires-python = ">=3.13,<3.14"`), installé par uv. | Wheels disponibles pour toute la pile (Django 5.2, psycopg, Celery) ; 3.14 encore trop récent pour une partie de l'écosystème. |
| 2026-11-12 | **CSP stricte dans tous les environnements** (`default-src 'none'`, `script-src 'self'`, aucun `unsafe-inline` ni `unsafe-eval`). | Le dev se comporte comme la prod : une violation CSP apparaît dès le développement, pas au déploiement. |
| 2026-11-12 | Cookies de session et CSRF préfixés **`__Host-`** dans `prod.py` uniquement ; noms par défaut en dev. | `__Host-` impose HTTPS, `path=/` et aucun domaine (un sous-domaine ne peut pas écraser le cookie) ; incompatible avec `http://localhost` en dev. |
| 2026-11-12 | `TimeStampedModel.created_by` : FK vers `AUTH_USER_MODEL`, `on_delete=PROTECT`, `null=True` (objets créés par le système), `related_name="+"`. **Un utilisateur ne se supprime pas, il se désactive** (`is_active=False`). | Garder l'auteur de chaque objet pour l'audit et l'archivage ; `PROTECT` lève `ProtectedError` plutôt que d'effacer ou d'orpheliner l'historique. |
| 2026-11-12 | **Tests sur PostgreSQL** (pas SQLite) ; pytest échoue avec un message explicite si la base est injoignable. | M2 repose sur un trigger PostgreSQL et des contraintes propres à PostgreSQL ; tester sur une autre base masquerait des erreurs. |
