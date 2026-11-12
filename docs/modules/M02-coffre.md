# M2 — Coffre : documents chiffrés et journal d'audit

## Objectif
Fournir aux autres modules un service unique pour stocker documents et fichiers
chiffrés, avec un niveau de confidentialité, et un journal d'audit infalsifiable.

## Modèles
- `Document(thesis, title, confidentiality, owner, current_version, related_object
  (GenericForeignKey facultatif))`
- `DocumentVersion(document, version, ciphertext_path, nonce, wrapped_dek, kek_id,
  algorithm, sha256_of_plaintext, size, mime_type, created_by)`
- `AuditEvent` : voir `docs/SECURITY.md`.

## Services (`apps/vault/services.py`)
- `store(thesis, uploaded_file, title, confidentiality, user) -> Document`
- `add_version(document, uploaded_file, user)`
- `open(document_version, user) -> bytes` (vérifie les droits, journalise)
- `audit(actor, action, obj, **details)` utilisé par tous les modules.
- Module `apps/vault/crypto.py` **court et isolé**, seul endroit qui importe les
  bibliothèques cryptographiques ; il ne fait qu'appeler leurs API de haut niveau.

## Critères d'acceptation
- [ ] Le fichier stocké sur disque n'est pas lisible en clair (test : le contenu
      original n'apparaît pas dans le blob).
- [ ] Modifier un octet du blob, du nonce ou des données associées fait échouer le
      déchiffrement (tests).
- [ ] Changer le niveau d'un document sans re-chiffrer sa DEK est impossible (les
      données associées incluent le niveau).
- [ ] Rotation de KEK : commande `manage.py rotate_kek <level>` qui re-chiffre les
      DEK ; les documents restent lisibles (test).
- [ ] Un rôle non autorisé pour le niveau reçoit 403 et l'essai est journalisé.
- [ ] `manage.py audit_verify` détecte une ligne modifiée dans la table (test).
- [ ] Validation des fichiers : taille, type par contenu (tests).
- [ ] L'admin Django n'affiche jamais de contenu déchiffré.
