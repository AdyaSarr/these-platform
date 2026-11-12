# M1 — Comptes, rôles, invitations, MFA

## Objectif
Chaque personne impliquée a un compte avec un rôle sur la thèse ; personne n'accède
à rien sans y avoir été invité.

## Modèles
- `Organization(name, kind: ST|LIRMM|UNIVERSITY|EXTERNAL)`
- `Thesis(title, cifre_start_date, first_enrollment_date, planned_end_date, status)`
- `Membership(user, thesis, role, organization, active_from, active_until)`
  unique (user, thesis, role)
- `Invitation(thesis, email, role, organization, token_hash, expires_at, used_at,
  invited_by)`

## Fonctionnalités
1. Le doctorant (super-rôle de la thèse) invite une personne : courriel + rôle.
2. Le courriel d'invitation contient seulement un lien à jeton (voir SECURITY.md).
3. L'invité crée son mot de passe puis **enrôle obligatoirement** un second facteur
   (TOTP au minimum ; WebAuthn si possible dans ce module, sinon en M1b).
4. `apps/accounts/permissions.py` : fonctions `can(user, action, obj)` et mixins de
   vues, fondés sur la matrice de `docs/ARCHITECTURE.md`.
5. Page « Équipe » : membres, rôles, organisation, date d'arrivée ; retrait d'un membre
   (sa `Membership` passe inactive, il perd l'accès immédiatement).
6. Limitation des tentatives de connexion.

## Critères d'acceptation
- [ ] Un utilisateur sans `Membership` active ne voit aucune thèse (test).
- [ ] Jeton d'invitation : usage unique, expiré après 72 h, stocké haché (tests).
- [ ] Impossible d'accéder au tableau de bord sans second facteur enrôlé (test).
- [ ] Pour chaque rôle de la matrice : un test « autorisé » et un test « refusé ».
- [ ] Un membre retiré reçoit 403 sur toutes les pages de la thèse (test).
- [ ] Les événements connexion, invitation, changement de rôle sont journalisés
      (journal simple en M1, branché sur le journal chaîné en M2).
