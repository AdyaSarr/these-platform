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
      l'invitation. La page du lien répond déjà `Referrer-Policy: strict-origin`
      (le POST « Continuer » ne porte donc que l'origine, pas l'URL), mais un navigateur ancien
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

- [ ] **Ordre de mise en service du coffre (M2).**
      ```bash
      manage.py migrate
      manage.py vault_init                                 # dossier des blobs 0700, KEK v1 de chaque domaine
      manage.py check --deploy --database default          # dont vault.E001 et vault.E002
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

- [ ] **Journal d'audit : rôles de base et points de contrôle (M2).**
      *Rôles* : les migrations sont lancées par le rôle **propriétaire** des tables ;
      le rôle de l'application n'a que `SELECT` et `INSERT` sur `vault_auditevent`
      et `vault_auditcheckpoint` (pas `UPDATE`, `DELETE`, `TRUNCATE`).
      **Le rôle applicatif n'est ni superutilisateur ni propriétaire des tables.**
      Les triggers ne protègent que contre un rôle qui ne peut pas les contourner :
      `SET session_replication_role = replica` (superutilisateur) désactive tous les
      triggers ordinaires de la session, et `ALTER TABLE … DISABLE TRIGGER`
      (propriétaire de la table) les désactive pour tous. Contre ces rôles, seuls
      les points de contrôle copiés hors de la plateforme protègent.
      *Vérifier* : `SELECT rolsuper FROM pg_roles WHERE rolname = current_user` →
      `f`, et `SELECT tableowner FROM pg_tables WHERE tablename = 'vault_auditevent'`
      ≠ le rôle de l'application.
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

- [ ] **Résumé hebdomadaire des objectifs (M4) : un timer le lundi.**
      *Timer systemd* (utilisateur du serveur), chaque lundi à 08 h 00 Europe/Paris
      (`OnCalendar=Mon *-*-* 08:00:00 Europe/Paris`, `Persistent=true` pour
      rattraper un lundi manqué) :
      ```
      manage.py objectives_weekly_summary
      ```
      La commande met les courriels en file (worker Celery démarré) ; la semaine
      résumée va du lundi précédent 00 h 00 au lundi 00 h 00, **heure de Paris**.
      Destinataires : directeur, co-encadrants, tuteur actifs, seulement s'il y a
      quelque chose sur les objectifs qu'ils peuvent lire (activité, retard, ou à
      prendre en connaissance pour le directeur). La relancer dans la semaine
      n'envoie rien deux fois (`WeeklySummaryLog`, contrainte d'unicité) ; un
      envoi en échec est retenté par les exécutions suivantes de la semaine, 3
      fois au plus. Courriel : un lien seulement.
      *Vérifier* : `systemctl list-timers` montre le timer ; la sortie de la
      commande ne contient que des nombres.

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
  `DJANGO_ALLOWED_HOSTS=these.example.org`, origines CSRF, `EMAIL_URL` SMTP et
  `DEFAULT_FROM_EMAIL` fictifs), prend un `VAULT_STORAGE_DIR` temporaire
  (`mktemp -d`, supprimé à la fin) et lance ce même contrôle avec
  `config.settings.prod`. Aucune variable ne reste dans le shell appelant, même
  si le script est sourcé ; les arguments supplémentaires vont au contrôle
  (`--database default`). Ce contrôle local ne remplace pas celui du serveur.
