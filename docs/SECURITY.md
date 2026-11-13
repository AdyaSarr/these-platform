# Sécurité

Référentiel : OWASP ASVS niveau 2, recommandations ANSSI. La PSSI de ST et du LIRMM
prime. Règle d'or : **aucune primitive maison**.

## Primitives retenues

| Usage | Choix | Bibliothèque | Module |
|---|---|---|---|
| Transport | TLS 1.3, `X25519MLKEM768` | OpenSSL 3.5+, Caddy/Nginx | déploiement |
| Mots de passe | Argon2id | `argon2-cffi` (hasher Django) | M0 |
| Second facteur | WebAuthn (clé d'accès), TOTP en secours | `webauthn`, `django-otp` | M1 |
| Chiffrement symétrique | XChaCha20-Poly1305 ou AES-256-GCM (AEAD) | `PyNaCl` / `cryptography` | M2 |
| Enveloppe de clés | DEK par document, KEK par niveau | `cryptography` | M2 |
| Journal d'audit | chaîne SHA-256 + signature Ed25519 quotidienne | `hashlib`, `PyNaCl` | M2 |
| Signatures de validation | Ed25519 puis hybride Ed25519 + ML-DSA-65 | `PyNaCl`, `liboqs-python` | M10 |
| Bout en bout | X25519 (+ ML-KEM-768 hybride) côté navigateur | libsodium.js, WebCrypto | M10 |

## Chiffrement par enveloppe (M2)

1. À l'enregistrement : générer une DEK aléatoire de 32 octets.
2. Chiffrer le contenu avec la DEK en AEAD ; **données associées** =
   `document_id || version || confidentiality_level || thesis_id`.
3. Chiffrer la DEK avec la KEK du niveau de confidentialité ; stocker
   `wrapped_dek`, `kek_id`, `nonce`, `algorithm`.
4. KEK : en développement, dérivées d'une clé maîtresse en variable d'environnement
   (`MASTER_KEY`, 32 octets en base64) via HKDF-SHA256 avec info = niveau ;
   en production, Vault/OpenBao (transit) ou HSM.
5. Rotation : nouvelle KEK => re-chiffrement des DEK seulement (pas des contenus).
6. L'interface d'administration Django ne doit jamais afficher un contenu déchiffré.

### Registre des KEK (`apps/vault/keys.py`)

- `kek_id = "<fournisseur>:<DOMAINE>:<version>"` (ex. `env:CONFIDENTIAL:3`),
  calculé par PostgreSQL (colonne générée) à partir des trois champs de
  `KeyEncryptionKey`, qui ne contient **aucun matériel de clé**.
- Chiffrement sous la version courante du domaine, avec `VAULT_KEY_PROVIDER` ;
  déchiffrement avec le fournisseur désigné par le préfixe du `kek_id`, s'il
  figure dans `VAULT_ENABLED_PROVIDERS`. Version retirée ou inconnue : refus.
- Verrous consultatifs PostgreSQL par domaine (`pg_advisory_xact_lock`, espace
  `0x564B`, clé = code figé du domaine) : **partagé** dans `wrap`, **exclusif**
  dans `new_version` et `retire`. Une rotation attend donc la validation de
  toute transaction qui chiffre sous l'ancienne version.
- `retire` refuse la version courante, une version encore référencée, et tout
  domaine sans fonction de comptage enregistrée (refus par défaut).

### Fournisseur de production prévu : OpenBao transit (conception, non implémenté)

- Une clé transit `aes256-gcm96` par domaine (`vault-kek-<domaine>`) ;
  `seal` = `transit/encrypt` avec `associated_data`, `open` = `transit/decrypt` ;
  la version de la clé transit est la `version` du `kek_id` (`bao:<DOMAINE>:<n>`).
- `new_version` = `transit/keys/<nom>/rotate` ; `min_decryption_version` relevé
  après `retire` pour que l'ancienne version ne puisse plus déchiffrer.
- Points de contrôle : clé transit `ed25519` (`transit/sign`, `transit/keys`
  pour la clé publique, exportée une seule fois).
- Authentification de l'application auprès d'OpenBao par AppRole, politique
  limitée à `encrypt`, `decrypt`, `sign` sur ces clés ; `rotate` réservé à un
  rôle d'administration utilisé par la commande de rotation.

### Ce qui est chiffré, ce qui ne l'est pas (M2)

| En clair (base de données) | Chiffré |
|---|---|
| **Titre** du document (avertissement sur le formulaire de dépôt) | Contenu de chaque version (blob AES-256-GCM sur disque) |
| Niveau de confidentialité | DEK de chaque version (`wrapped_dek`, sous la KEK du niveau) |
| Taille, type MIME, numéro de version | Graine TOTP et graine des codes de récupération (KEK `MFA_SECRETS`) |
| Dates de création et de modification, auteur, thèse | |
| Chemin du blob (nom aléatoire), `kek_id` | |

Le titre n'apparaît jamais dans le journal d'audit, l'admin, `str()` d'un
document, les journaux ni les noms de fichiers stockés (tests). Le nom du
fichier téléchargé est reconstruit depuis le titre nettoyé
(`[a-z0-9-]`, 80 caractères au plus) et l'extension du type détecté.

### Textes scellés (M4, `vault.SealedText`)

Les textes libres de recherche (intitulé, description et critère d'un objectif,
motifs et valeurs des révisions, notes d'avancement, commentaires) ne sont
jamais stockés en clair. Une ligne `SealedText` par couple (emplacement, objet) :

1. **Emplacement (slot)** : le couple modèle + champ (`objectives.Objective.title`)
   codé sur 2 octets, **figé** dans `formats.TEXT_SLOTS` (plages réservées par
   module dans `formats.py` ; un code retiré n'est jamais réattribué, un test le
   vérifie).
2. **Bourrage ISO/IEC 7816-4** du texte UTF-8 (`0x80` puis des zéros) jusqu'au
   multiple de **64 octets** suivant, avant chiffrement ; 16 383 octets au plus.
   Au déchiffrement, tout bourrage que `pad_text` ne peut pas produire est une
   `VaultIntegrityError`. Le bourrage est vérifié **après** le tag AEAD : ce
   n'est pas un oracle de bourrage.
3. **AEAD** (AES-256-GCM) sous une **DEK neuve à chaque écriture** ; données
   associées `0x01 | 0x04 | thesis_id | object_id | slot | niveau` (37 octets) ;
   DEK encapsulée sous la KEK du niveau avec
   `0x01 | 0x05 | thesis_id | object_id | slot | niveau | len | kek_id`.
4. **Écriture** par `vault.services.sealing(...)` : droits (action métier ET
   accès au niveau) vérifiés hors transaction et refus journalisé, puis
   transaction et verrou partagé de la KEK **avant** les verrous de lignes de
   l'appelant ; l'appelant appelle `audit()` en dernier dans le bloc ; un
   `seal()` après `audit()` lève `LockOrderError`.
5. **Lecture** par `open_texts(...)` : une requête par page ; un texte d'un
   niveau que le lecteur ne voit pas revient `RESTRICTED` (« Contenu
   restreint ») ; la lecture d'un texte `CONFIDENTIAL` est journalisée
   (`SEALED_TEXT_READ`, un événement par appel, emplacements et identifiants
   seulement) ; la session ouverte avec second facteur suffit pour l'afficher
   (pas de second facteur récent, contrairement au téléchargement d'un
   document `CONFIDENTIAL`).
6. **Rotation** : `rewrap_batch` ré-encapsule aussi les DEK des textes ; un
   compteur de références par domaine empêche de retirer une KEK encore utilisée.

**Ce qui reste visible** pour qui lit la base : thèse, emplacement, objet,
niveau, `kek_id`, dates, auteur, et la **longueur du texte à 64 octets près**
(tranche `⌈(n+1)/64⌉`). Limite assumée : quelqu'un qui peut écrire dans la
base peut remettre une **ancienne** valeur du même emplacement (comme pour les
documents) ; l'historique des révisions et le journal d'audit le rendent visible.
Les contraintes de base refusent un chiffré dont la longueur n'est pas celle
d'un texte bourré, et une `kek_id` d'un autre domaine que le niveau.

## Dossier administratif (M3)

| En clair (base de données) | Chiffré (coffre, M2) |
|---|---|
| Code et libellé des pièces du catalogue (affiché depuis `catalog.py`) | Pièces jointes, au niveau par défaut de la pièce |
| **Libellé** des pièces personnalisées (avertissement sur le formulaire) | |
| Statut, échéance, validateurs, approbations, motif (liste fermée) | |
| **Commentaire** de refus ou de réouverture (avertissement, 280 caractères) | |

- Libellés personnalisés et commentaires n'apparaissent jamais dans le journal
  d'audit (le code de la pièce et le code du motif seulement), `str()`, les
  courriels, les journaux applicatifs et Celery ; aucun modèle du dossier n'est
  inscrit dans l'admin Django (tests de `tests/test_security_audit_m3.py`).
- **Restriction de section** (`permissions.register_document_restriction`) : une
  pièce jointe n'est lisible qu'avec le droit de lecture de la section de sa pièce
  (`CIFRE_FILE_READ` ou `ADMIN_FILE_READ`), en plus du rôle et du niveau ; ni une
  autorisation explicite ni un niveau abaissé ne la contournent.
- **Séparation des fonctions** : une approbation par rôle **et** par personne et
  par cycle, jamais par le déposant du cycle (contraintes en base) ; seule la
  version déposée est approuvée.
- **Rappels** : courriel sans contenu (un lien), identifiant de `ReminderLog`
  seul dans le courtier et les journaux, `EMAIL_TIMEOUT = 10` (une ligne de rappel
  verrouillée ne reste jamais bloquée par un serveur SMTP muet).

## Objectifs (M4)

- **Niveaux** d'un objectif : `PUBLIC` (par défaut ; le formulaire avertit qu'il
  est visible par le CSI, extérieur à l'encadrement), `INTERNAL_LIRMM`,
  `CONFIDENTIAL`. `INTERNAL_ST` est refusé (le directeur, qui approuve, ne le lit
  pas). Révisions, notes et commentaires suivent le niveau de leur objectif ; un
  changement de niveau rescelle toutes ces dépendances.
- **En clair** : thèse, année, catégorie, échéance, poids, statut, avancement,
  niveau, approbation et prise de connaissance (qui, quand), marques « modifié »
  et « résultat déclaré », valeurs non textuelles d'une révision (date, poids,
  niveau, statut). **Scellé** : intitulé, description, critère, motif, anciennes
  et nouvelles valeurs textuelles, notes, commentaires. Les statistiques
  n'ouvrent aucun texte (test).
- **Droits** : écrire = doctorant ; approuver et prendre connaissance = directeur,
  second facteur de moins de 10 min (vue et service) ; commenter et lire les
  commentaires = `OBJECTIVE_COMMENT` (jamais le CSI : ni la page, ni le fragment,
  ni le courriel) ; un rôle sans accès au niveau lit « Contenu restreint ».
- **Journal** : noms de champs et nombres seulement ; une seule lecture
  `CONFIDENTIAL` journalisée par requête (`SEALED_TEXT_READ`).
- **Résumé hebdomadaire** : un lien seulement, envoyé à un encadrant s'il y a de
  l'activité, un retard ou (directeur) une prise de connaissance sur les
  objectifs qu'il peut lire ; le courtier ne voit que l'identifiant.
- **Titres** jamais dans une URL, un `<title>`, un journal, une page 403 ni un
  courriel (tests).

## Authentification

- Identifiant = courriel ; hasher Django `Argon2PasswordHasher` en premier.
- MFA obligatoire pour tous ; WebAuthn recommandé.
- TOTP : période de 30 s, **tolérance d'un pas** (`MFA_TOTP_TOLERANCE = 1`,
  RFC 6238 § 5.2). La fenêtre d'allauth est symétrique : les codes des pas
  k-1, k et k+1 sont acceptés (90 s). Chaque code est à usage unique sur toute
  la fenêtre (marqueur dans le cache partagé) ; l'étape du second facteur est
  limitée à 5 essais. Probabilité qu'un essai au hasard réussisse : 3 × 10⁻⁶.
- Ré-authentification (< 10 min) pour : valider (dossier administratif :
  approuver, refuser, rouvrir), changer un rôle, télécharger un document
  `CONFIDENTIAL`, exporter un rapport. Le mot de passe seul ne suffit pas.
- Limitation des tentatives de connexion (`django-axes` ou équivalent).
- Invitations : jeton aléatoire 32 octets, stocké haché, usage unique, 72 h.

## Paramètres Django (prod)

`DEBUG=False`, `ALLOWED_HOSTS` explicite, `SECURE_SSL_REDIRECT`,
`SECURE_HSTS_SECONDS=31536000` + `INCLUDE_SUBDOMAINS` + `PRELOAD`,
`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE="Strict"`,
`SESSION_COOKIE_AGE` court (8 h), `X_FRAME_OPTIONS="DENY"`,
`SECURE_REFERRER_POLICY="same-origin"`, CSP via `django-csp` (pas d'inline script).

## Journal d'audit (M2)

`AuditEvent(seq, timestamp, actor, thesis, action, object_type, object_id, ip_hash,
details_json, prev_hash, hash)`, en ajout seul (`apps/vault/audit.py`).

- **Chaîne** : `hash = SHA-256(prev_hash (32 octets bruts) ‖ JSON_canonique(charge))`,
  `prev_hash` initial = 32 octets nuls ; charge = `{"v": 1, "seq", "timestamp",
  "actor", "thesis", "action", "object_type", "object_id", "ip_hash", "details"}`.
- **JSON canonique** (`formats.canonical_json`) : sous-ensemble de RFC 8785 (pas
  de flottants, entiers dans ±(2⁵³ − 1), clés triées par unités UTF-16, aucun
  espace). Horodatage `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC, 6 chiffres), UUID en
  minuscules avec tirets, `ip_hash` en hexadécimal minuscule. Clés de `details`
  en `[a-z][a-z0-9_]*` ; tout champ texte haché refuse NUL et surrogates isolés.
- **`seq`** contigu à partir de 1, attribué par `audit()` sous le verrou
  consultatif du journal (`0x5641`, 1), dernier appel de la transaction.
- **Triggers PostgreSQL** : UPDATE, DELETE et TRUNCATE refusés ; un INSERT doit
  prolonger la chaîne (`seq = dernier + 1`, `prev_hash = dernier hash`) et prend
  le même verrou que `audit()` (les insertions SQL directes sont mises en file).
  Mêmes protections pour `AuditCheckpoint`, dont le haché doit être celui de son
  événement.
- **`manage.py audit_verify --public-key <clé épinglée> --checkpoints <export>`** :
  recalcule la chaîne (comparaisons en temps constant) ; détecte une
  modification (haché), une suppression (trou de `seq`), une insertion au milieu
  (chaînage rompu) ; contre les points de contrôle externes, une réécriture
  complète et une **troncature de la fin**, indétectable sans eux (avertissement
  explicite). Un horodatage qui recule est une information, pas une altération.
- **Points de contrôle** (`apps/vault/checkpoints.py`) : signature Ed25519 de
  `b"these-audit-checkpoint-v1\0" ‖ seq (8 o) ‖ hash (32 o) ‖ horodatage (µs, 8 o)`
  par la clé dérivée de `CHECKPOINT_INFO`. La clé de signature est sur le serveur
  de la chaîne : un point de contrôle ne protège qu'une fois **copié hors de la
  plateforme** (export quotidien récupéré par une machine extérieure, courriel
  hebdomadaire au doctorant : seule exception à la règle 7).

### Clé publique des points de contrôle

- `manage.py audit_checkpoint --export-public-key` affiche la clé et son empreinte
  SHA-256 ; chaque export est journalisé et peut être refait (c'est une clé
  publique, recalculable par le serveur).
- **La clé de référence est celle du premier export, conservée hors de la
  plateforme.** `audit_verify` ne l'accepte qu'en paramètre, jamais lue en base
  ni dans un courriel. Une clé différente lors d'un export ultérieur est une
  **alerte** (serveur compromis ou `MASTER_KEY` changée), jamais un remplacement.
- **Changement légitime** (rotation de `MASTER_KEY`, passage à OpenBao) : nouvelle
  étiquette `CHECKPOINT_INFO = b"audit-checkpoint-ed25519-v2"` dans `crypto.py`,
  dernier point de contrôle signé par l'ancienne clé, puis premier point de
  contrôle signé par la nouvelle, la nouvelle clé exportée et conservée hors de
  la plateforme à côté de l'ancienne, et la date du changement consignée dans
  `docs/decisions.md`. `audit_verify` vérifie chaque période avec sa clé.

## Fichiers déposés

Taille max (25 Mo), liste blanche de types (PDF, PNG, JPEG, CSV, TXT, ZIP, TEX),
vérification par contenu (pas seulement l'extension), antivirus ClamAV en prod,
téléchargement via vue Django avec `Content-Disposition: attachment`.

- **Aucun dépôt n'est jamais écrit en clair sur disque.** `FILE_UPLOAD_HANDLERS`
  ne contient que `apps.vault.upload_handlers.LimitedMemoryUploadHandler` :
  réception en mémoire, interrompue (`StopUpload`) dès que le total de la
  requête dépasse 25 Mo ; `TemporaryFileUploadHandler` est exclu, car il écrirait
  les dépôts de plus de 2,5 Mo dans le répertoire temporaire avant tout
  chiffrement. Le proxy limite aussi la taille du corps de requête.
- Validation (`apps/vault/validation.py`) : 25 Mo comptés sur les octets lus,
  type reconnu par signature et égal à celui de l'extension, texte en UTF-8
  strict sans caractère de contrôle ; `Content-Type` et nom fournis par le
  client ignorés (le nom téléchargé est reconstruit depuis le titre).
- Blobs (`apps/vault/storage.py`) : `<2 hex>/<32 hex>.blob`, nom tiré du CSPRNG ;
  écriture `O_EXCL` en 0600 (dossiers 0700), `fsync` (`F_FULLFSYNC` sur macOS),
  publication par `os.link` (jamais d'écrasement, refus si le système de
  fichiers n'a pas de liens physiques), `fsync` du dossier ; lecture par
  descripteurs de dossier avec `O_NOFOLLOW` à chaque niveau. `vault_gc` liste
  orphelins, blobs manquants et anomalies, sans rien supprimer ni lire.

## Chaîne logicielle

`pre-commit` : ruff, bandit, detect-secrets. CI : tests, `pip-audit`, Semgrep.
Dépendances épinglées dans `uv.lock`, mises à jour via Renovate.

## Hors périmètre tant que l'hébergement n'est pas validé

Aucune donnée scientifique réelle (traces, résultats ST) dans l'instance de
développement : utiliser des données fictives.
