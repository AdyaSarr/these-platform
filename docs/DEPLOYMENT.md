# Déploiement

Aucun déploiement avant la validation de l'hébergement par ST et le LIRMM
(`docs/ROADMAP.md`). Cette page rassemble ce qui doit être vrai **avant** la
première mise en production. Elle se complète au fil des modules.

Les fichiers d'exemple sont dans `deploy/examples/` (Caddyfile, unité systemd de
gunicorn, `redis.conf`, rôles PostgreSQL). Tout comportement d'un outil (Caddy,
gunicorn, PostgreSQL, Redis, restic) est **à vérifier sur la version déployée**,
avec la commande donnée ; « vérifié le … » ne vaut que pour la version citée.

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
      les en-têtes `Referer` et `Cookie` de ses journaux d'accès : bloc `log` de
      `deploy/examples/Caddyfile` (expression `request>uri regexp
      ^/(invitation|ical)/[A-Za-z0-9_-]{20,} /${1}/[masque]`). Le serveur
      d'application n'a pas de journal d'accès (case « Serveur d'application »).

      *Pourquoi* : le jeton est dans l'URL du lien envoyé par courriel ; tant qu'il
      n'est pas utilisé (72 h), quiconque lit les journaux peut accepter
      l'invitation. La page du lien répond déjà `Referrer-Policy: strict-origin`
      (le POST « Continuer » ne porte donc que l'origine, pas l'URL), mais un navigateur ancien
      ou une extension peut l'ignorer. Django ne journalise que des jetons déjà
      invalides (« Bad Request: /invitation/… »).
      *Vérifier* : `caddy validate --config /etc/caddy/Caddyfile` sur la version
      de Caddy déployée (la syntaxe de `format filter` a évolué en 2.8 ; le
      Caddyfile d'exemple est valide pour Caddy v2.11.9, vérifié le 2026-11-18,
      syntaxe seulement) ; ouvrir un lien d'invitation puis
      `grep -c <jeton> /var/log/caddy/these-access.log` **et**
      `journalctl -u these-gunicorn | grep -c <jeton>` : `0` et `0`.

- [ ] **Jetons des flux iCal absents des journaux d'accès du proxy (M12).**
      La règle fusionnée ci-dessus couvre les deux chemins
      (`/invitation/<jeton>/` et `/ical/<jeton>.ics`) ; à confirmer par
      `caddy validate` et le `grep -c` ci-dessous sur la version déployée. *Pourquoi* : le jeton est un secret porteur, sans second
      facteur, valable 12 mois ; une application d'agenda le relit toutes les
      heures, chaque requête écrirait l'URL complète. Côté Django, le filtre
      `TokenPathFilter` masque déjà le chemin dans toutes les lignes.
      *Vérifier* : créer un lien d'abonnement, le lire une fois
      (`curl -s -o /dev/null <url>`), puis, pour chaque journal (proxy, serveur
      d'application) :
      ```bash
      re='/\(ical\|invitation\)/[A-Za-z0-9_-]\{20,\}'
      grep -c "$re" /var/log/caddy/these-access.log            # 0
      journalctl -u these-gunicorn --no-pager | grep -c "$re"  # 0
      ls /var/log | grep -ci gunicorn                          # 0 : aucun fichier de journal
      ```

- [ ] **Vraie adresse IP du client transmise à Django (M1, SR-02).**
      `prod.py` fixe `ALLAUTH_TRUSTED_PROXY_COUNT = 1` : un seul proxy (Caddy)
      devant Django, et l'adresse du client est la **dernière** entrée de
      `X-Forwarded-For`, celle qu'ajoute ce proxy ; les entrées précédentes
      viennent du client et sont ignorées. Les limites d'allauth (échecs de
      connexion), l'`ip_hash` du journal d'audit et la limite par adresse du flux
      iCal lisent toutes cette adresse par la même fonction
      (`vault.audit.client_ip`).
      *Pourquoi* : derrière un proxy, `REMOTE_ADDR` vaut l'adresse du proxy pour
      tout le monde : dix échecs par minute d'un seul attaquant bloqueraient la
      connexion de tous, et tous les `ip_hash` seraient identiques.
      *À vérifier sur la version de Caddy déployée* (ne pas le supposer) : que
      Caddy ajoute l'adresse du pair à `X-Forwarded-For` et qu'aucun autre proxy
      ne s'intercale (sinon, ajuster `ALLAUTH_TRUSTED_PROXY_COUNT`).
      *Vérifier* : depuis une machine dont l'adresse publique est connue, une
      connexion échouée portant un `X-Forwarded-For` forgé (jeton CSRF compris,
      sinon Django refuse la requête avant toute tentative) :
      ```bash
      url=https://these.example.org/accounts/login/; jar="$(mktemp)"
      tok="$(curl -s -c "$jar" "$url" | sed -n 's/.*name="csrfmiddlewaretoken" value="\([^"]*\)".*/\1/p')"
      curl -s -o /dev/null -b "$jar" -H "Referer: $url" -H 'Origin: https://these.example.org' \
           -H 'X-Forwarded-For: 198.51.100.66' --data-urlencode "csrfmiddlewaretoken=$tok" \
           -d 'login=inconnu@example.org' -d 'password=x' "$url"; rm -f "$jar"
      ```
      Puis, sur le serveur, comparer l'`ip_hash` de cet échec au pseudonyme de
      l'adresse réelle et à celui de l'adresse forgée :
      ```bash
      manage.py shell -c "
      from apps.vault import crypto; from apps.vault.models import AuditEvent
      e = AuditEvent.objects.filter(action='LOGIN_FAILED').latest('seq')
      print(e.ip_hash == crypto.pseudonym('<adresse réelle>', purpose='ip'),
            e.ip_hash == crypto.pseudonym('198.51.100.66', purpose='ip'))"
      ```
      Attendu : `True False`. `False True` : l'en-tête du client est cru (Caddy
      le transmet sans y ajouter l'adresse du pair, ou trop de proxys déclarés).
      `False False` : l'adresse lue est celle d'un proxy (un second proxy
      s'intercale, ou l'en-tête n'arrive pas à Django).

- [ ] **gunicorn ajouté aux dépendances (SR-19).** Il ne l'est pas encore :
      `uv add gunicorn` dans un commit à part (suite complète à `exit=0`,
      `uv run pip-audit` sans vulnérabilité), avant le premier déploiement.
      *Vérifier* : `grep -c '^name = "gunicorn"' uv.lock` → `1` ;
      `uv run gunicorn --version` sur le serveur.

- [ ] **Serveur d'application : gunicorn, sans journal d'accès, sur
      `127.0.0.1` (SR-19).** Unité systemd : `deploy/examples/these-gunicorn.service`.
      Commande exacte :
      ```bash
      gunicorn config.wsgi:application \
          --bind 127.0.0.1:8000 \
          --worker-class gthread --workers 2 --threads 4 \
          --timeout 60 --graceful-timeout 30 \
          --max-requests 1000 --max-requests-jitter 100
      ```
      - **Pas de `--access-logfile`** : le chemin des requêtes contient les jetons
        d'invitation (`/invitation/<jeton>/`) et de flux iCal (`/ical/<jeton>.ics`),
        que ni le `TokenPathFilter` de Django ni le filtre de Caddy ne verraient dans
        le journal d'accès de gunicorn. *À vérifier sur la version déployée* :
        gunicorn n'écrit aucun journal d'accès sans cette option, aucun fichier
        `gunicorn.conf.py` n'est lu depuis `WorkingDirectory`, et
        `GUNICORN_CMD_ARGS` n'est pas défini (cette variable ajoute des options).
        Son journal d'erreurs (stderr, donc journald) peut citer le chemin d'une
        requête dont l'exception sort de Django (*à vérifier* ; le `grep -c`
        ci-dessous le couvre).
      - **`127.0.0.1` seulement** : Caddy est le seul client ; gunicorn n'est
        jamais joignable depuis le réseau.
      - **`gthread`, 2 × 4** : au plus 8 requêtes dans Django à la fois, ce qui borne
        la mémoire des dépôts (case « Taille et nombre des dépôts »). Un client lent
        n'occupe qu'un fil, pas un processus. *À vérifier sur la version
        déployée* : la concurrence de `gthread` est bien `workers × threads`.
      - Ne pas activer `--proxy-protocol` ni modifier `--forwarded-allow-ips` :
        l'adresse du client vient de `X-Forwarded-For`, lue par Django (SR-02).
      *Vérifier* :
      ```bash
      ss -ltnp | grep ':8000 '                                  # 127.0.0.1:8000 seulement
      test ! -e /srv/these/gunicorn.conf.py && echo absent      # absent
      grep -c GUNICORN_CMD_ARGS /etc/these/these.env            # 0
      gunicorn --help | grep -e '--print-config'                # si l'option existe :
      gunicorn config.wsgi:application --print-config | grep -E '^(accesslog|bind|worker_class|workers|threads) '
      # attendu : accesslog = None, bind = ['127.0.0.1:8000'], gthread, 2, 4
      ```
      puis le `grep -c` des jetons dans `journalctl -u these-gunicorn` (case
      « Jetons des flux iCal ») : `0`.

- [ ] **Secrets MFA scellés par le coffre (M2).**
      La graine TOTP et la graine des codes de récupération passent par
      `MFAAdapter.encrypt()`, qui délègue à `vault.services.encrypt_secret`
      (AES-256-GCM sous la KEK `MFA_SECRETS`).
      *Pourquoi* : sans chiffrement, une lecture de la base suffit pour générer
      les codes TOTP et de récupération de tout le monde, donc contourner le
      second facteur. La prod **refuse de démarrer** si l'adaptateur configuré
      n'est pas `MFAAdapter` (ou une sous-classe qui redéfinit `encrypt` ou
      `decrypt`) : contrôle structurel sans base (`apps/accounts/guards.py`,
      appelé par `config/wsgi.py` et `config/asgi.py`). L'aller-retour réel est
      vérifié sur la base déployée par `vault.E001` (ci-dessous).
      *Vérifier* : `python -c "import config.wsgi"` avec l'environnement de prod
      se termine sans erreur ; aucune valeur `secret` ou `seed` de
      `mfa_authenticator.data` ne commence autrement que par `vault1:`
      (`manage.py vault_encrypt_mfa_secrets` scelle celles d'une base antérieure
      à M2 et doit afficher `0 secret(s) sealed`).

- [ ] **PostgreSQL : deux rôles, mot de passe, écoute locale (SR-20).**
      Rôles et droits : `deploy/examples/postgres-roles.sql`.
      - `theses_owner` possède la base et les tables, et **lance seul les
        migrations** (`DATABASE_URL` du propriétaire dans un fichier
        d'environnement à part, `/etc/these/these-migrate.env`, root 0600, lu par
        la procédure de déploiement seulement) ; après **chaque** `migrate`, la
        partie 2 du script redonne les droits (idempotente).
      - `theses_app` est le rôle de gunicorn et de Celery : ni superutilisateur, ni
        propriétaire, ni membre du propriétaire ; `SELECT` et `INSERT` seulement
        sur `vault_auditevent` et `vault_auditcheckpoint`. C'est ce que contrôle
        `vault.E003` (case « Journal d'audit »).
      - `theses_backup` lit tout (`pg_read_all_data`), pour `pg_dump` seulement.
      - Mots de passe : générés (`secrets.token_urlsafe(32)`), posés par
        `\password` dans `psql`, stockés en SCRAM (`password_encryption =
        scram-sha-256`) ; `pg_hba.conf` n'accepte que `scram-sha-256` pour ces
        rôles, depuis `127.0.0.1/32` et `::1/128` (ou la socket locale).
      - Écoute : `listen_addresses = 'localhost'` (ou `''`, socket seule). Garder
        un `DATABASE_URL` en `postgres://…@127.0.0.1:5432/theses` : la forme
        socket n'a pas été essayée avec la validation de `DATABASE_URL`
        (`config/settings/base.py`). Base sur une autre machine : TLS obligatoire
        (`?sslmode=verify-full`, à vérifier avec cette même validation).
      Vérifié le 2026-11-18 sur PostgreSQL 16.15 (image de développement épinglée,
      serveur jetable) : script parties 1 à 3, `migrate` sous `theses_owner`,
      `vault_init`, `check --deploy --database default` (aucune erreur `vault.*`) et
      `audit_checkpoint` sous `theses_app` ; `DELETE` et `TRUNCATE` du journal
      refusés à `theses_app` ; `vault.E003` levé sous `theses_owner` ; `pg_dump`
      sous `theses_backup` puis `pg_restore` : mêmes lignes et mêmes déclencheurs.
      *À refaire sur la version déployée*, et un parcours complet (connexion,
      dépôt, validation) sous `theses_app` en préproduction : il n'a été fait que
      par les tests, sous le rôle de test.
      *Vérifier* :
      ```sql
      SHOW listen_addresses;                                    -- localhost (ou vide)
      SELECT rolname, rolsuper, rolpassword LIKE 'SCRAM-SHA-256$%'
        FROM pg_authid WHERE rolname LIKE 'theses_%';           -- f, t pour chacun
      SELECT type, database, user_name, address, auth_method
        FROM pg_hba_file_rules;                                 -- scram-sha-256, local seulement
      ```
      et `ss -ltn | grep ':5432 '` → `127.0.0.1` et `[::1]` seulement.

- [ ] **Redis : mot de passe, écoute locale, persistance, mémoire bornée
      (SR-20, SR-15).** Configuration : `deploy/examples/redis.conf`. Redis
      porte les compteurs des limites de débit, les marqueurs anti-rejeu TOTP
      (cache Django) et la file Celery (identifiants seulement) : un Redis
      joignable sans mot de passe permettrait de remettre les limites à zéro et
      de rejouer un code TOTP.
      - **Mot de passe** : `requirepass` dans un fichier inclus,
        `/etc/redis/these-password.conf` (root:redis 0640), jamais sur une ligne
        de commande ; le même dans `REDIS_URL` (`redis://:<mot de passe>@127.0.0.1:6379/0`).
      - **Écoute** : `bind 127.0.0.1 -::1`, `protected-mode yes`.
      - **Persistance** : `appendonly yes` (la file Celery survit à un redémarrage).
      - **Mémoire** : `maxmemory 256mb`, `maxmemory-policy volatile-lru` (SR-15 :
        un balayage de jetons iCal inventés ne remplit plus la mémoire). Seules
        les clés **avec TTL** (le cache Django) sont évincées, jamais la file
        Celery (`allkeys-lru` pourrait évincer ses clés, qui n'ont pas de TTL :
        à vérifier, ci-dessous). Effet assumé d'une éviction : un compteur de
        limite ou un marqueur anti-rejeu peut disparaître avant son terme ;
        `INFO stats` → `evicted_keys` doit rester à `0` en temps normal.
      - **Redis sur une autre machine** : `port 0`, `tls-port`, certificats, et
        `REDIS_URL=rediss://…` (accepté par `config/settings/base.py`). *À vérifier*
        avec la version de Celery déployée : les paramètres de certificat qu'elle
        exige dans une URL `rediss://`.
      Le développement applique déjà le fichier de configuration et la même
      politique (`docker-compose.yml`, vérifié le 2026-11-18 sur Redis 7.4.12 :
      `NOAUTH` sans mot de passe, `volatile-lru`).
      *Vérifier* (sur la version déployée ; `REDISCLI_AUTH` évite le mot de passe
      sur la ligne de commande) :
      ```bash
      ss -ltn | grep ':6379 '                                   # 127.0.0.1 et [::1] seulement
      redis-cli ping                                            # NOAUTH Authentication required.
      read -rs REDISCLI_AUTH; export REDISCLI_AUTH
      redis-cli config get maxmemory-policy                     # volatile-lru
      redis-cli config get maxmemory                            # 268435456
      redis-cli info persistence | grep aof_enabled             # aof_enabled:1
      redis-cli --scan --pattern '_kombu*' | while read -r k; do redis-cli ttl "$k"; done   # -1 partout
      unset REDISCLI_AUTH
      ```

- [ ] **Ordre de mise en service du coffre (M2).**
      ```bash
      manage.py migrate                                    # rôle propriétaire, puis partie 2 de postgres-roles.sql
      manage.py vault_init                                 # dossier des blobs 0700, KEK v1 de chaque domaine
      manage.py check --deploy --database default          # dont vault.E001 à vault.E003
      manage.py create_thesis ...                          # refuse tant que vault_init n'a pas été lancé
      ```
      *Pourquoi* : la première connexion d'un utilisateur active son second
      facteur, dont la graine est scellée sous la KEK `MFA_SECRETS` : sans
      `vault_init`, l'activation échoue. `vault.E001` fait un aller-retour réel
      de chiffrement MFA ; `vault.E002` signale un domaine sans KEK. Ces
      contrôles, étiquetés `database`, ne s'exécutent qu'avec `--database default`.
      *Vérifier* : la commande `check` ci-dessus affiche « System check
      identified no issues ».

- [ ] **Années de thèse après la migration de M4.**
      ```bash
      manage.py migrate                                    # dont objectives.0001
      manage.py objectives_sync_years --all                # idempotent
      ```
      *Pourquoi* : les thèses créées après M4 reçoivent leurs années (CIFRE et
      universitaires) par le `post_save` de `Thesis` ; celles créées avant
      n'en ont aucune, et leurs objectifs ne pourraient être rattachés à rien.
      La migration n'importe pas `sync_years` (code métier hors migration).
      *Vérifier* : une seconde exécution affiche « 0 created, 0 updated,
      0 deleted » ; toute ligne « kept » (année en trop gardée parce que des
      objectifs la référencent) est examinée avant de modifier la date de fin.

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

- [ ] **Taille et nombre des dépôts limités par le proxy (M2, SR-14).**
      Django n'écrit jamais un dépôt sur disque : `LimitedMemoryUploadHandler`
      est le gestionnaire **de tout le projet** (`FILE_UPLOAD_HANDLERS`), et garde
      en mémoire tout corps multipart, sur **n'importe quelle** route, jusqu'à
      `MAX_UPLOAD_BYTES` = 25 Mio (26 214 400 octets). Un POST anonyme sur
      `/accounts/login/`, avec un en-tête `Origin` du site, est lu par le contrôle
      CSRF avant d'être refusé. Caddy (`deploy/examples/Caddyfile`) :
      - **les cinq routes de dépôt** (les vues qui lisent `request.FILES` :
        `documents/new/`, `documents/<id>/versions/new/`,
        `admin-file/<id>/submit/`, `travel/missions/<id>/official-order/`,
        `travel/expenses/<id>/receipt/`, sous `/t/<thèse>/`) : corps de
        `26MiB` au plus, et **au plus deux connexions** vers Django
        (`transport http { max_conns_per_host 2 }`) ;
      - **toutes les autres routes** : corps de `1MiB` au plus.
      Mémoire des corps dans Django : au plus 2 × 25 Mio + 6 × 1 Mio (8 requêtes
      à la fois, case « Serveur d'application »).
      **Une nouvelle vue de dépôt** doit être ajoutée à l'expression `@upload` ;
      oubliée, elle répond 413 au-delà de 1 Mio (visible), jamais l'inverse.
      `tests/test_caddy_upload_routes.py` échoue dans les deux sens : une vue qui lit
      `request.FILES` hors de `@upload`, ou une alternative qui ne désigne plus une
      route de dépôt.
      **`MiB`, pas `MB`** : Caddy v2.11.9 lit `26MB` comme 26 000 000 octets,
      moins que 25 Mio (vérifié le 2026-11-18 par `caddy adapt` : 26 000 000 pour
      `26MB`, 27 262 976 pour `26MiB`) ; la limite `26MB` écrite ici jusqu'au
      lot C aurait refusé, avec cette version, un fichier de 25 Mio.
      *À vérifier sur la version déployée* : les tailles lues
      (`caddy adapt --config /etc/caddy/Caddyfile | grep -o '"max_size":[0-9]*'`
      → `27262976` et `1048576`) ; que `max_conns_per_host` fait **attendre** une
      troisième requête de dépôt (corps non lu) au lieu de la refuser ; que Caddy ne
      met pas le corps en mémoire tampon (pas de `request_buffers`).
      *Vérifier* (sans trafic, depuis une machine extérieure) :
      ```bash
      head -c 2000000 /dev/urandom > /tmp/2m; head -c 26000000 /dev/urandom > /tmp/25m
      o='Origin: https://these.example.org'
      curl -s -o /dev/null -w '%{http_code}\n' -H "$o" -F f=@/tmp/2m https://these.example.org/accounts/login/   # 413
      z=00000000-0000-0000-0000-000000000000
      for i in 1 2 3; do curl -s -o /dev/null -w '%{http_code}\n' --limit-rate 200k -H "$o" \
          -F f=@/tmp/25m "https://these.example.org/t/$z/documents/new/" & done
      ```
      et, sur le serveur, pendant l'envoi :
      `ss -Htn state established '( dport = :8000 )' | wc -l` → `2` au plus.

- [ ] **Sauvegardes chiffrées et cohérentes de la base et des blobs (M2, SR-16).**
      **Les deux vont dans un même dépôt restic chiffré**, hors du serveur
      (`sftp:` ou stockage objet) : jamais de `pg_dump` en clair sur un disque ni
      dans un répertoire sauvegardé autrement. Base d'abord, puis le dossier des
      blobs : un blob présent sans sa ligne est un orphelin inoffensif, l'inverse
      serait un document illisible. Timer systemd quotidien, utilisateur dédié
      `these-backup` (mot de passe de `theses_backup` dans son `~/.pgpass`, 0600) :
      ```bash
      export RESTIC_REPOSITORY=sftp:sauvegarde@backup.example.org:/srv/restic/these
      export RESTIC_PASSWORD_FILE=/etc/these/restic-password    # root:these-backup 0440
      restic backup --tag db --stdin-filename theses.dump \
          --stdin-from-command -- pg_dump -Fc -h 127.0.0.1 -U theses_backup -d theses
      restic backup --tag blobs /var/lib/these/vault              # VAULT_STORAGE_DIR
      restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune   # durée : Q8
      ```
      *À vérifier sur la version de restic déployée* : que le dépôt est chiffré
      et authentifié (conception de restic, `restic version`) ;
      `restic backup --help | grep stdin-from-command` (sans cette option, un
      `pg_dump | restic backup --stdin` masque l'échec de `pg_dump` : alors
      `set -o pipefail` et contrôle du code de sortie) ; que `restic restore`
      rend les droits 0700/0600 des blobs (`vault_gc` le signale sinon).
      **Alternative sans restic** : `pg_dump -Fc … | age -r <clé publique> >
      theses-<date>.dump.age`, avec seule la clé publique sur le serveur (le
      serveur ne relit pas ses propres sauvegardes) ; mêmes règles de lieu et de
      conservation.
      **Où vit la clé de sauvegarde** : le mot de passe du dépôt restic est sur le
      serveur (`RESTIC_PASSWORD_FILE`, lisible par root et `these-backup`
      seulement, jamais dans Git ni dans `these.env`), et **une copie hors ligne**
      est tenue par le doctorant (gestionnaire de mots de passe) : sans elle, une
      perte du serveur rend les sauvegardes illisibles. **`MASTER_KEY` (puis
      OpenBao) n'est jamais dans le dépôt restic** et se garde séparément, par
      quelqu'un d'autre ou ailleurs : qui a la sauvegarde **et** `MASTER_KEY` lit
      tout.
      **Ce que contient la sauvegarde** (revue 2026-11, domaine 11) :
      - *en clair dans le dump* : prénoms, noms et adresses des comptes et des
        invitations ; organismes, rôles et dates des adhésions ; titre et dates de
        la thèse ; titre, niveau, type et taille des documents (y compris
        `CONFIDENTIAL`) ; libellés et commentaires (refus, réouverture, échéance)
        du dossier administratif ; type, date, durée, lieu et niveau des
        réunions ; conférences et échéances ; jours de présence avec leur nature
        (dont les **congés**), dates, destination et payeur des missions ; journal
        d'audit (identifiants, codes, `ip_hash`, `email_hmac`) ; sessions ;
      - *scellé* : textes de recherche, montants, motifs et lieux précis des
        missions, titres des soumissions, secrets MFA, et chaque `wrapped_dek`
        (DEK encapsulée par une KEK **absente** de la base) ;
      - *blobs* : documents chiffrés (AEAD), illisibles sans leur DEK.
      Le chiffrement de restic protège donc d'abord la partie en clair.
      **Test de restauration** (avant la mise en production, puis chaque
      trimestre et après tout changement de la procédure), sur le serveur, dans
      une base et un dossier jetables jamais exposés :
      ```bash
      restic snapshots --tag db
      restic dump --tag db latest /theses.dump > /var/tmp/restore/theses.dump
      createdb -O theses_owner theses_restore
      pg_restore --no-owner --role=theses_owner -d theses_restore /var/tmp/restore/theses.dump
      sed -n '/^-- 2\./,/^-- 3\./p' deploy/examples/postgres-roles.sql \
          | psql -U theses_owner -d theses_restore      # partie 2 : droits de theses_app
      restic restore --tag blobs latest --target /var/tmp/restore/blobs
      # environnement de prod, DATABASE_URL vers theses_restore, VAULT_STORAGE_DIR vers les blobs restaurés :
      manage.py check --deploy --database default    # no issues
      manage.py vault_gc                             # code 0 : aucun blob MISSING, aucune ANOMALY
      manage.py audit_verify --public-key <clé> --checkpoints <copie de l'export>   # code 0
      ```
      puis ouvrir un document `CONFIDENTIAL` restauré (aller-retour réel de la
      DEK), et supprimer `theses_restore` et `/var/tmp/restore`. *À vérifier* :
      le chemin du dump dans l'instantané (`restic ls --tag db latest`). Vérifié
      le 2026-11-18 sur PostgreSQL 16.15 (serveur jetable, sans restic) : `pg_dump
      -Fc` sous `theses_backup` puis `pg_restore` gardent les lignes du journal et
      ses déclencheurs.
      **Effacement réel = durée de conservation des sauvegardes** : l'effacement
      d'un ancien blob (après `change_level`, ou blob orphelin) n'est effectif
      qu'à l'expiration de la dernière sauvegarde de la base contenant sa
      `wrapped_dek`. La durée de conservation des sauvegardes (`restic forget`)
      est le délai d'effacement réel, à annoncer à ST et au LIRMM (Q8).

- [ ] **Journal d'audit : rôles de base et points de contrôle (M2).**
      *Rôles* (`deploy/examples/postgres-roles.sql`, case « PostgreSQL ») : les
      migrations sont lancées par le rôle **propriétaire** des tables ;
      le rôle de l'application n'a que `SELECT` et `INSERT` sur `vault_auditevent`
      et `vault_auditcheckpoint` (pas `UPDATE`, `DELETE`, `TRUNCATE`).
      **Le rôle applicatif n'est ni superutilisateur ni propriétaire des tables.**
      Les triggers ne protègent que contre un rôle qui ne peut pas les contourner :
      `SET session_replication_role = replica` (superutilisateur) désactive tous les
      triggers ordinaires de la session, et `ALTER TABLE … DISABLE TRIGGER`
      (propriétaire de la table) les désactive pour tous. Contre ces rôles, seuls
      les points de contrôle copiés hors de la plateforme protègent.
      *Vérifier* : `manage.py check --deploy --database default` (contrôle
      `vault.E003`, SR-07) échoue si le rôle de l'application est superutilisateur,
      propriétaire de `vault_auditevent` ou de `vault_auditcheckpoint`, ou membre du
      rôle propriétaire. À la main : `SELECT rolsuper FROM pg_roles WHERE rolname =
      current_user` → `f`, et `SELECT tableowner FROM pg_tables WHERE tablename =
      'vault_auditevent'` ≠ le rôle de l'application.
      *Timers systemd* (environnement de prod, utilisateur du serveur) :
      ```
      # chaque jour
      manage.py audit_checkpoint --export /var/lib/these/export/checkpoints.jsonl
      # chaque semaine
      manage.py audit_checkpoint --email
      # chaque semaine, sur la machine extérieure qui détient la clé épinglée
      manage.py audit_verify --public-key <clé> --checkpoints <copie de l'export>
      ```
      **Le fichier d'export doit QUITTER le serveur** : il est récupéré par une
      machine extérieure (modèle « pull », par exemple `scp` lancé depuis la
      machine du doctorant, avec une clé SSH en lecture seule sur ce seul fichier).
      Un export resté sur le serveur ne protège rien : qui réécrit la chaîne peut
      aussi réécrire l'export.
      *Clé publique* : `audit_checkpoint --export-public-key` une fois à
      l'initialisation ; noter la clé et son empreinte hors de la plateforme
      (`docs/SECURITY.md`).
      *Vérifier* : `audit_verify` avec la clé épinglée et la copie de l'export
      sort avec le code 0, sans avertissement de troncature.

- [ ] **Rappels du dossier administratif (M3) : un timer quotidien.**
      *Timer systemd* (utilisateur du serveur), chaque jour à 08 h 00 Europe/Paris
      (`OnCalendar=*-*-* 08:00:00 Europe/Paris`, `Persistent=true` pour rattraper
      un jour manqué) :
      ```
      manage.py admin_file_reminders
      ```
      La commande met les courriels en file (worker Celery démarré, même
      `DJANGO_SETTINGS_MODULE`) ; les fenêtres J-14, J-3 et J+1 sont des **dates à
      Paris** (`TIME_ZONE`). La relancer à la main le même jour, ou en même temps
      que le timer, n'envoie rien deux fois (`ReminderLog`, contrainte d'unicité).
      Un envoi en échec est retenté par les exécutions suivantes, 3 fois au plus.
      *Vérifier* : `systemctl list-timers` montre le timer ; la sortie de la
      commande ne contient que des nombres (ni libellé, ni adresse).

- [ ] **Courriels des réunions (M5a) : un timer quotidien.**
      *Timer systemd* (utilisateur du serveur), chaque jour à 07 h 00 Europe/Paris
      (`OnCalendar=*-*-* 07:00:00 Europe/Paris`, `Persistent=true`) :
      ```
      manage.py meetings_reminders
      ```
      Rappel des réunions du lendemain aux participants, action en retard (J+1,
      une fois) à son responsable, nouvel essai des courriels en échec (3 au plus).
      Dates à Paris ; la relancer n'envoie rien deux fois (`MeetingNotification`,
      contrainte d'unicité). *Vérifier* : la sortie ne contient que des nombres.

- [ ] **Rappels des conférences (M6) : un timer quotidien.**
      *Timer systemd*, chaque jour à 07 h 00 Europe/Paris (`Persistent=true`) :
      ```
      manage.py conferences_reminders
      ```
      Fenêtres J-30, J-14, J-7, J-1 comptées en dates **dans le fuseau de chaque
      échéance** (AoE, IANA) ; date limite de demande d'accord ST en dates de Paris ;
      accords en attente rappelés au tuteur (J-7, J-1). La relancer n'envoie rien deux
      fois. *Vérifier* : la sortie ne contient que des nombres.

- [ ] **Rappels des déplacements (M7) : un timer quotidien.**
      *Timer systemd*, chaque jour à 07 h 00 Europe/Paris (`Persistent=true`) :
      ```
      manage.py travel_reminders
      ```
      Accord de mission à demander (une semaine avant le délai `order_lead_days`,
      puis à ce délai), accord en attente rappelé à la personne du payeur (J-7, J-1
      avant le départ), justificatifs ou montants réels manquants après le retour.
      Dates à Paris ; la relancer n'envoie rien deux fois (`TravelNotification`,
      contrainte d'unicité) ; à l'envoi, destinataire revérifié et rappel encore dû
      (mission annulée, accord décidé, justificatif déposé entre-temps : rien
      n'est envoyé). *Vérifier* : la sortie ne contient que des nombres.

- [ ] **Résumé hebdomadaire des objectifs (M4) : timer à désactiver (M11b).**
      Le résumé du lundi est fondu dans les résumés d'activité (`activity_digests`) ;
      la commande `objectives_weekly_summary` n'existe plus. Désactiver et supprimer
      son timer (`systemctl disable --now` puis retirer l'unité). Les lignes de
      `WeeklySummaryLog` restent en lecture et sont purgées à 13 mois par
      `activity_purge`.

- [ ] **Activité (M11a) : deux timers quotidiens.**
      *Timers systemd* (utilisateur du serveur) :
      ```
      manage.py activity_digests    # chaque jour à 07 h 00 Europe/Paris (Persistent=true)
      manage.py activity_purge      # chaque jour, à une heure creuse
      ```
      `activity_digests` écrit les objectifs en retard (une fois chacun), met en
      file les résumés quotidiens et, le lundi, les résumés hebdomadaires (M11b :
      ils regroupent aussi les courriels différés de M3, M5a, M6 et M7 ; un lien
      vers la page « Résumé » seulement), et remet en file les courriels
      d'activité en échec (3 tentatives au plus). La relancer
      n'envoie rien deux fois. `activity_purge` supprime les événements de plus
      de 13 mois, avec leurs lectures et leurs courriels.
      *Vérifier* : la sortie des deux commandes ne contient que des nombres.

- [ ] **Flux iCal (M12) : un timer quotidien.**
      *Timer systemd*, chaque jour à 06 h 30 Europe/Paris (`Persistent=true`) :
      ```
      manage.py feeds_daily
      ```
      Révoque les liens expirés (12 mois) et ceux des thèses soutenues ou closes
      (journalisé, raison seule) ; écrit, 14 jours avant l'expiration, un avis
      « Échéances » (une fois par lien ; le courriel suit les préférences, un lien
      seulement). La relancer ne fait rien deux fois. *Vérifier* : la sortie ne
      contient que des nombres.

- [ ] **Fin de thèse (M13a) : un timer quotidien.**
      *Timer systemd*, chaque jour à 06 h 45 Europe/Paris (`Persistent=true`) :
      ```
      manage.py lifecycle_daily
      ```
      Expire les demandes de clôture de plus de 14 jours, termine les adhésions dues
      d'une thèse close (rattrapage des fins manquées à la clôture, puis le
      directeur à la fin de sa période de lecture) ; journalisé, codes seulement. La
      relancer ne fait rien deux fois. *Vérifier* : la sortie ne contient que des
      nombres.

- [ ] **Export du doctorant (M13b) : longues réponses en flux.**
      L'export est une réponse en flux (`StreamingHttpResponse`, `Content-Disposition:
      attachment`), jusqu'à `EXPORT_MAX_BYTES` (2 Gio) ; un fil `gthread` reste
      occupé pendant tout le téléchargement. *À vérifier sur la version déployée* :
      que Caddy ne met pas la réponse en mémoire tampon et qu'aucun délai d'écriture
      de Caddy ni de gunicorn (`--timeout`, concerne le battement du processus avec
      `gthread`) ne coupe un téléchargement long.
      *Vérifier* : un export d'environ 500 Mio téléchargé par
      `curl --limit-rate 2M` va au bout et `unzip -t` répond « No errors detected » ;
      l'historique de la page de l'export dit « Complet ».

## Rappels de configuration

- `EMAIL_TIMEOUT = 10` (base et prod) : un serveur SMTP qui ne répond pas fait
  échouer l'envoi au bout de 10 s (rappel compté `FAILED`, retenté le lendemain)
  au lieu de bloquer un worker Celery et la ligne de rappel verrouillée.

- `DJANGO_SETTINGS_MODULE=config.settings.prod` dans l'environnement du serveur
  web **et** du worker Celery. `dev` et `test` refusent de démarrer si
  `DJANGO_ALLOWED_HOSTS` contient le domaine public (`config/settings/guards.py`).
- Secrets uniquement par variables d'environnement ; `prod.py` ne lit aucun fichier
  `.env`. Liste des variables : `.env.example`.
- `manage.py check --deploy --settings=config.settings.prod --fail-level WARNING`
  doit répondre `no issues` sur le serveur, avec son environnement réel.
  Avant, sur le poste de développement : `scripts/check_deploy.sh` (lancé par le
  développeur seulement, il lit `.env`). Il charge `.env` dans un sous-shell
  `( set -a; . ./.env; … )`, impose des valeurs fictives de production
  (`DJANGO_SITE_URL=https://these.example.org`,
  `DJANGO_ALLOWED_HOSTS=these.example.org`, origines CSRF, `EMAIL_URL` SMTP (`smtp+tls://`) et
  `DEFAULT_FROM_EMAIL` fictifs), prend un `VAULT_STORAGE_DIR` temporaire
  (`mktemp -d`, supprimé à la fin) et lance ce même contrôle avec
  `config.settings.prod`. Aucune variable ne reste dans le shell appelant, même
  si le script est sourcé ; les arguments supplémentaires vont au contrôle
  (`--database default`). Ce contrôle local ne remplace pas celui du serveur.
