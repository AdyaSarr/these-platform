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
- [x] Un utilisateur sans `Membership` active ne voit aucune thèse (test).
      *Preuve* : `test_user_without_active_membership_sees_no_thesis`,
      `test_home_without_thesis_lists_nothing`, `test_user_without_membership_is_refused`.
- [x] Jeton d'invitation : usage unique, expiré après 72 h, stocké haché (tests).
      *Preuve* : `test_invitation_is_single_use`, `test_concurrent_acceptances_succeed_only_once`,
      `test_token_expires_after_72_hours`, `test_invitation_is_sent_with_hashed_token`,
      `test_clear_token_is_stored_nowhere`, `test_check_token_compares_in_constant_time`.
- [x] Impossible d'accéder au tableau de bord sans second facteur enrôlé (test).
      *Preuve* : `test_dashboard_unreachable_without_second_factor`,
      `test_every_thesis_route_requires_a_second_factor`,
      `test_password_alone_does_not_sign_in_an_enrolled_user`.
- [x] Pour chaque rôle de la matrice : un test « autorisé » et un test « refusé ».
      *Preuve* : `test_can_role_action` (6 rôles × 24 actions), `test_can_access_level`
      (6 × 4), et par vue : `test_thesis_views.py`, `test_team_actions.py`.
- [x] Un membre retiré reçoit 403 sur toutes les pages de la thèse (test).
      *Preuve* : `test_removed_member_gets_403_everywhere_with_existing_session`
      (toutes les routes `/t/…`, GET et POST, session conservée).
- [x] Les événements connexion, invitation, changement de rôle sont journalisés
      (journal simple en M1, branché sur le journal chaîné en M2).
      *Preuve* : `test_account_audit.py` (connexion réussie ou échouée, déconnexion,
      rôle, retrait, prolongation, attribution, second facteur),
      `test_invitation_lifecycle_is_audited`, `test_revocation_is_audited`.

## Revue de fin (2026-11-12)
Audit de sécurité : `tests/test_security_audit_m1.py` (IDOR, escalade, fixation de
session, redirection ouverte, CSRF sur toutes les routes POST, limitation de l'étape
TOTP, injection d'en-têtes, compte désactivé). Une faille trouvée et corrigée : le
service d'invitation acceptait une adresse contenant CR/LF (l'injection était
bloquée par Django à l'envoi, mais une invitation invalide était créée).
