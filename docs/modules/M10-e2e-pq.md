# M10 — Chiffrement de bout en bout et signatures post-quantiques

## Objectif
Pour le niveau `CONFIDENTIAL`, le serveur ne voit jamais le clair. Les validations
importantes portent une signature hybride classique + post-quantique.

**Module sensible : faire relire la conception par un encadrant avant de coder.**

## Bout en bout
- Chaque membre génère dans son navigateur une paire de clés X25519 (libsodium.js) ;
  la clé privée est chiffrée par une clé dérivée de sa phrase de passe (Argon2id
  côté client) ou protégée par WebAuthn PRF si disponible.
- Un document est chiffré côté client (XChaCha20-Poly1305) avec une DEK, encapsulée
  pour chaque destinataire (`crypto_box_seal`). Hybride X25519 + ML-KEM-768 dès
  qu'une implémentation navigateur auditée est disponible.
- Ajout d'un membre : ré-encapsulation par un membre existant. Retrait : nouvelle
  DEK pour les versions suivantes.
- Contreparties documentées : pas de recherche côté serveur, perte de la phrase de
  passe = perte d'accès (prévoir une clé de récupération imprimée).

## Signatures hybrides
- Clés Ed25519 + ML-DSA-65 par validateur, `liboqs-python` côté serveur pour la
  vérification.
- Signature valide seulement si **les deux** signatures sont valides.
- Message signé = JSON canonique `{thesis, object_type, object_id, version_hash,
  action, timestamp}`.

## Critères d'acceptation
- [ ] Le serveur ne reçoit que des chiffrés pour `CONFIDENTIAL` (test d'intégration).
- [ ] Une signature hybride dont une moitié est invalide est refusée (test).
- [ ] Vecteurs de test officiels utilisés pour vérifier l'intégration ML-DSA.
