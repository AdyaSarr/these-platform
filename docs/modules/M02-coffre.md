# M2 — Coffre : documents chiffrés et journal d'audit

## Objectif
Fournir aux autres modules un service unique pour stocker documents et fichiers
chiffrés, avec un niveau de confidentialité, et un journal d'audit infalsifiable.

## Modèles
- `Document(thesis, title, confidentiality, owner, current_version, related_object
  (GenericForeignKey facultatif))`
- `DocumentVersion(document, version, ciphertext_path, wrapped_dek, kek_id, size,
  mime_type, created_by)` : ni `nonce` ni `algorithm` (l'en-tête versionné du blob
  en tient lieu), ni `sha256_of_plaintext` (oracle de confirmation), voir
  `docs/decisions.md`.
- `DocumentAccessGrant(document, user, granted_by, revoked_at)` : la « liste
  explicite » de `CONFIDENTIAL`.
- `KeyEncryptionKey` (métadonnées des KEK, `kek_id` calculé par PostgreSQL),
  `AuditEvent` (chaîné) et `AuditCheckpoint` : voir `docs/SECURITY.md`.

## Services (`apps/vault/services.py`)
- `store(thesis, uploaded_file, title, confidentiality, user) -> Document`
- `add_version(document, uploaded_file, user)`
- `open_version(version_id, user, *, thesis=None, recent_mfa=False) -> Opened`
  (vérifie les droits et la thèse de l'URL, exige un second facteur récent pour
  `CONFIDENTIAL`, journalise), `change_level`, `grant_access`, `revoke_access`,
  `encrypt_secret` / `decrypt_secret` (secrets MFA), `rewrap_batch` (rotation)
- `audit(action, *, actor=None, obj=None, thesis=None, request=None, **details)`
  utilisé par tous les modules (créé en M1, chaîné en M2, désormais dans
  `apps/vault/audit.py` et réexporté par `services`) :
  - `action` : membre de `AuditAction` (une chaîne quelconque est refusée) ;
  - `thesis` est déduite de `obj` si elle n'est pas fournie ; si les deux sont
    fournies et diffèrent, `AuditError` ;
  - `actor=None` autorisé (connexion échouée, tâche système) ;
  - `subject_email=` : le courriel concerné (ex. connexion échouée) n'est jamais
    stocké en clair, seulement son HMAC salé après normalisation, comme `ip_hash` ;
  - `details` refuse toute clé contenant un fragment interdit (mot de passe,
    jeton, secret, code TOTP ou de récupération, clé, session, cookie, courriel),
    à toute profondeur, et toute valeur non sérialisable en JSON.
- Module `apps/vault/crypto.py` **court et isolé**, seul endroit qui importe les
  bibliothèques cryptographiques ; il ne fait qu'appeler leurs API de haut niveau.

## Critères d'acceptation
- [x] Le fichier stocké sur disque n'est pas lisible en clair (test : le contenu
      original n'apparaît pas dans le blob).
- [x] Modifier un octet du blob, du nonce ou des données associées fait échouer le
      déchiffrement (tests).
- [x] Changer le niveau d'un document sans re-chiffrer sa DEK est impossible (les
      données associées incluent le niveau).
- [x] Rotation de KEK : commande `manage.py rotate_kek <level>` qui re-chiffre les
      DEK ; les documents restent lisibles (test).
- [x] Un rôle non autorisé pour le niveau reçoit 403 et l'essai est journalisé.
- [x] `manage.py audit_verify` détecte une ligne modifiée dans la table (test).
- [x] Validation des fichiers : taille, type par contenu (tests).
- [x] L'admin Django n'affiche jamais de contenu déchiffré.
- [x] Points de contrôle Ed25519 : `audit_checkpoint --export-public-key` exporte la
      clé publique et son empreinte (ré-export journalisé ; la clé de référence est
      celle du premier export, conservée hors de la plateforme) ; `audit_verify` prend cette clé épinglée en
      paramètre (jamais une clé lue en base ou dans un courriel) et une liste de
      points de contrôle externes, et détecte une troncature de la fin (tests).
- [x] Le courriel de point de contrôle, envoyé au seul `PHD_STUDENT`, ne contient que
      `(seq, hash, signature, horodatage)` (test) ; export complémentaire vers un
      fichier externe.

## Clôture (2026-11-13)
Critères vérifiés un par un avec leurs tests lors de la revue de fin de M2 ; audit
de sécurité : `tests/test_security_audit_m2.py`.

## Notes issues de M1
- Le rapport CSI est stocké au niveau `CONFIDENTIAL`, que `LEVEL_ROLES` ne donne
  pas au `CSI_MEMBER`. Il y accédera par la **« liste explicite »** du niveau
  `CONFIDENTIAL` : `can_access_level()` doit accepter une autorisation par
  document (utilisateur explicitement listé), en plus des rôles de `LEVEL_ROLES`.
  *Réalisé* par `can_access_document()` (`can_access_level()` reste la règle de
  niveau) et `DocumentAccessGrant`, valable tant que la `Membership` est active.
