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

## Authentification

- Identifiant = courriel ; hasher Django `Argon2PasswordHasher` en premier.
- MFA obligatoire pour tous ; WebAuthn recommandé.
- Ré-authentification (< 10 min) pour : valider, changer un rôle, télécharger un
  document `CONFIDENTIAL`, exporter un rapport.
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
details_json, prev_hash, hash)` avec
`hash = SHA-256(prev_hash || canonical_json(champs))`. Ajout seul (pas d'UPDATE ni
DELETE : contrôlé par le code et par un trigger PostgreSQL). Commande
`manage.py audit_verify` qui recalcule la chaîne.

## Fichiers déposés

Taille max (25 Mo), liste blanche de types (PDF, PNG, JPEG, CSV, TXT, ZIP, TEX),
vérification par contenu (pas seulement l'extension), antivirus ClamAV en prod,
téléchargement via vue Django avec `Content-Disposition: attachment`.

## Chaîne logicielle

`pre-commit` : ruff, bandit, detect-secrets. CI : tests, `pip-audit`, Semgrep.
Dépendances épinglées dans `uv.lock`, mises à jour via Renovate.

## Hors périmètre tant que l'hébergement n'est pas validé

Aucune donnée scientifique réelle (traces, résultats ST) dans l'instance de
développement : utiliser des données fictives.
