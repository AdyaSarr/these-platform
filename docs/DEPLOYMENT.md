# Déploiement

Aucun déploiement avant la validation de l'hébergement par ST et le LIRMM
(`docs/ROADMAP.md`). Cette page rassemble ce qui doit être vrai **avant** la
première mise en production. Elle se complète au fil des modules.

## Checklist avant mise en production

Chaque case est bloquante : on ne déploie pas tant qu'une seule reste vide.

- [ ] **CI obligatoire avant tout déploiement.** Un pipeline lance au minimum
      `uv run pytest`, `detect-secrets` et `uv run pip-audit` sur la révision
      déployée, et le déploiement exige qu'il soit vert.
      *Pourquoi* : les hooks pre-commit se contournent par `git commit --no-verify` ;
      le test `test_detect_secrets_finds_nothing_new_in_tracked_files` ne protège que
      s'il est exécuté.
      *Vérifier* : le dernier pipeline de la révision déployée est vert, et la
      protection de branche interdit de fusionner sans lui.

- [ ] **`collectstatic` dans la procédure de déploiement.**
      `uv run python manage.py collectstatic --noinput --settings=config.settings.prod`
      à chaque déploiement, avant de redémarrer Django.
      *Pourquoi* : la prod utilise `ManifestStaticFilesStorage` ; sans manifeste,
      toute page qui utilise `{% static %}` répond 500.
      *Vérifier* : `var/static/staticfiles.json` existe et la page de connexion
      répond 200.

- [ ] **Caddy (ou Nginx) transmet l'en-tête `Host` d'origine** et l'en-tête
      `X-Forwarded-Proto`.
      *Pourquoi* : `ALLOWED_HOSTS` ne protège que si Django reçoit le vrai `Host` ;
      `SECURE_PROXY_SSL_HEADER` s'appuie sur `X-Forwarded-Proto` pour savoir que la
      requête est en HTTPS. Le proxy doit aussi **écraser** un `X-Forwarded-Proto`
      envoyé par le client.
      *Vérifier* : `curl -H "Host: autre.example" https://<domaine>/health/` répond
      400 ; `curl https://<domaine>/health/` répond 200.

- [ ] **MFA (M1) terminé et obligatoire** avant toute ouverture à un utilisateur.
      *Pourquoi* : jusqu'à M1, la connexion se fait par mot de passe seul (la route
      `/admin/` n'existe pas en prod, mais `/accounts/login/` oui).
      *Vérifier* : M1 est coché dans `docs/ROADMAP.md`, et un compte sans second
      facteur enrôlé n'accède à aucune page de thèse.

- [ ] **Téléchargements en `Content-Disposition: attachment` + `nosniff` (M2).**
      *Pourquoi* : la CSP autorise `script-src 'self'` ; un fichier déposé servi
      en ligne depuis notre domaine pourrait être exécuté comme script.
      *Vérifier* : un test de M2 contrôle ces deux en-têtes sur la vue de
      téléchargement ; aucun fichier déposé n'est servi par le proxy en statique.

## Rappels de configuration

- `DJANGO_SETTINGS_MODULE=config.settings.prod` dans l'environnement du serveur
  web **et** du worker Celery. `dev` et `test` refusent de démarrer si
  `DJANGO_ALLOWED_HOSTS` contient le domaine public (`config/settings/guards.py`).
- Secrets uniquement par variables d'environnement ; `prod.py` ne lit aucun fichier
  `.env`. Liste des variables : `.env.example`.
- `manage.py check --deploy --settings=config.settings.prod --fail-level WARNING`
  doit répondre `no issues` sur le serveur, avec son environnement réel.
