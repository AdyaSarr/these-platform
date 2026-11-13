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

- [ ] **Jetons d'invitation absents des journaux d'accès du proxy (M1).**
      Caddy masque le jeton dans le chemin `/invitation/<jeton>/` et supprime
      l'en-tête `Referer` de ses journaux d'accès :

      ```caddy
      these.example.org {
      	log {
      		output file /var/log/caddy/these-access.log {
      			roll_keep_for 720h
      		}
      		format filter {
      			wrap json
      			fields {
      				# Jetons : 43 caractères base64url ; /invitation/accept/ reste lisible.
      				request>uri regexp ^/invitation/[A-Za-z0-9_-]{20,}/ /invitation/[masque]/
      				request>headers>Referer delete
      				request>headers>Cookie delete
      			}
      		}
      	}
      	reverse_proxy 127.0.0.1:8000
      }
      ```

      *Pourquoi* : le jeton est dans l'URL du lien envoyé par courriel ; tant qu'il
      n'est pas utilisé (72 h), quiconque lit les journaux peut accepter
      l'invitation. La page du lien répond déjà `Referrer-Policy: no-referrer`
      (le POST « Continuer » ne porte donc pas l'URL), mais un navigateur ancien
      ou une extension peut l'ignorer. Django ne journalise que des jetons déjà
      invalides (« Bad Request: /invitation/… »).
      *Vérifier* : `caddy validate --config /etc/caddy/Caddyfile` sur la version
      de Caddy déployée (la syntaxe de `format filter` a évolué en 2.8) ; ouvrir un
      lien d'invitation puis `grep <jeton> /var/log/caddy/these-access.log` : aucune
      occurrence.

- [ ] **Vraie adresse IP du client transmise à Django (M1).** Le proxy fixe
      `REMOTE_ADDR` (ou un en-tête de confiance lu par un middleware dédié) à
      l'adresse du client, en écrasant toute valeur envoyée par celui-ci.
      *Pourquoi* : `ip_hash` du journal d'audit et la limitation des tentatives
      de connexion s'appuient sur l'adresse du client ; derrière un proxy,
      `REMOTE_ADDR` vaut sinon `127.0.0.1` pour tout le monde.
      *Vérifier* : deux connexions depuis deux machines produisent deux `ip_hash`
      différents dans `vault_auditevent`.

- [ ] **Secrets MFA chiffrés : `MFAAdapter.encrypt()` branché sur le coffre (M2).**
      La graine TOTP et la graine des codes de récupération passent par
      `apps/accounts/adapters.py:MFAAdapter.encrypt()`, qui est l'identité en M1.
      *Pourquoi* : sans chiffrement, une lecture de la base suffit pour générer
      les codes TOTP et de récupération de tout le monde, donc contourner le
      second facteur. La prod **refuse de démarrer** tant que `encrypt()` est
      l'identité (`apps/accounts/guards.py`, appelé par `config/wsgi.py` et
      `config/asgi.py`, `MFA_ENCRYPTION_REQUIRED = True` dans `prod.py`).
      *Vérifier* : `python -c "import config.wsgi"` avec l'environnement de prod
      se termine sans erreur ; `test_prod_refuses_to_serve_while_encrypt_is_the_identity`
      a été remplacé en M2 par un test du chiffrement réel.

- [ ] **Téléchargements en `Content-Disposition: attachment` + `nosniff` (M2).**
      *Pourquoi* : la CSP autorise `script-src 'self'` ; un fichier déposé servi
      en ligne depuis notre domaine pourrait être exécuté comme script.
      *Vérifier* : un test de M2 contrôle ces deux en-têtes sur la vue de
      téléchargement ; aucun fichier déposé n'est servi par le proxy en statique.

- [ ] **KEK hors du serveur applicatif : OpenBao (transit) au lieu de `env` (M2).**
      *Pourquoi* : avec le fournisseur `env`, les KEK sont dérivées de `MASTER_KEY`,
      présente dans l'environnement du serveur ; qui lit cet environnement lit
      tout le coffre. Avec OpenBao, la KEK ne quitte jamais OpenBao.
      *Procédure* : implémenter `OpenBaoTransitProvider` (conception dans
      `docs/SECURITY.md`), ajouter `"bao"` à `VAULT_ENABLED_PROVIDERS` et fixer
      `VAULT_KEY_PROVIDER = "bao"`, puis `rotate_kek <domaine>` pour chaque domaine
      (documents et `MFA_SECRETS`) ; une fois **toutes** les anciennes versions
      `env:…` retirées (`retire` refuse tant qu'une ligne les référence), **retirer
      `"env"` de `VAULT_ENABLED_PROVIDERS`**.
      *Vérifier* : `SELECT kek_id FROM vault_keyencryptionkey WHERE retired_at IS NULL`
      ne renvoie que des `bao:…` ; le serveur démarre avec
      `VAULT_ENABLED_PROVIDERS = frozenset({"bao"})`.

- [ ] **Dossier des blobs privé et jamais servi par le proxy (M2).**
      `VAULT_STORAGE_DIR` absolu, hors de toute racine de Caddy (aucun
      `file_server` ni `root` dessus), propriété du compte du serveur, mode 0700 ;
      la prod refuse de démarrer sinon (`VAULT_STORAGE_STRICT`). Système de
      fichiers **avec liens physiques** (ext4, XFS ; pas de partage SMB/FAT) :
      l'écriture s'y appuie et refuse de fonctionner sans.
      *Vérifier* : `curl -I https://<domaine>/<chemin vers un blob>` renvoie 404 ;
      `manage.py vault_gc` sort avec le code 0.

- [ ] **Taille du corps de requête limitée par le proxy (M2).**
      `request_body { max_size 26MB }` dans Caddy (25 Mo de fichier + en-têtes
      multipart). Django n'écrit jamais un dépôt sur disque
      (`LimitedMemoryUploadHandler`) et cesse de le conserver au-delà de 25 Mo ;
      le proxy borne ce qu'il lit et jette au-delà.

- [ ] **Sauvegardes cohérentes de la base et des blobs (M2).** Base d'abord
      (`pg_dump`), puis le dossier des blobs (restic) : un blob présent sans sa
      ligne est un orphelin inoffensif, l'inverse serait un document illisible.
      **Effacement réel = durée de conservation des sauvegardes** : l'effacement
      d'un ancien blob (après `change_level`, ou blob orphelin) n'est effectif
      qu'à l'expiration de la dernière sauvegarde de la base contenant sa
      `wrapped_dek`. La durée de conservation des sauvegardes est le délai
      d'effacement réel, à annoncer à ST et au LIRMM.

## Rappels de configuration

- `DJANGO_SETTINGS_MODULE=config.settings.prod` dans l'environnement du serveur
  web **et** du worker Celery. `dev` et `test` refusent de démarrer si
  `DJANGO_ALLOWED_HOSTS` contient le domaine public (`config/settings/guards.py`).
- Secrets uniquement par variables d'environnement ; `prod.py` ne lit aucun fichier
  `.env`. Liste des variables : `.env.example`.
- `manage.py check --deploy --settings=config.settings.prod --fail-level WARNING`
  doit répondre `no issues` sur le serveur, avec son environnement réel.
