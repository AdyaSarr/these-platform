# M3 — Dossier administratif

## Objectif
Savoir d'un coup d'œil si toute la paperasse est faite, par qui, et ce qui manque.
La thèse passe à `ACTIVE` quand toutes les pièces obligatoires sont validées.

## Modèles (`apps/admin_file/models.py`)
- `RequiredItem(thesis, code, label, section, validator_roles, default_level,
  mandatory, attachment_required, due_date, status: TODO|SUBMITTED|VALIDATED|REJECTED,
  cycle, document (vault, unique), submitted_version, submitted_by, validated_at,
  reason_code, reason_comment)`.
  - `section` : `CIFRE` (lue avec `CIFRE_FILE_READ` : doctorant, directeur, tuteur)
    ou `ACADEMIC` (`ADMIN_FILE_READ` : doctorant, directeur).
  - `validator_roles` : `[DIRECTOR]`, `[INDUSTRIAL_TUTOR]` ou les deux (seules
    valeurs permises en base). Le déposant est toujours le doctorant.
  - `label` vide pour une pièce du catalogue (affichée depuis le catalogue),
    libellé en clair pour une pièce personnalisée (`custom-<12 hex>`).
- `ItemApproval(item, cycle, role, user, submitted_by, version)` : unique par
  (pièce, cycle, rôle) **et** par (pièce, cycle, utilisateur), jamais par le
  déposant du cycle.
- `ReminderLog(item, kind: J14|J3|OVERDUE, due_date, recipient, status, attempts)` :
  unique par (pièce, type, échéance, destinataire).
- Pas de migration de données : le catalogue (`catalog.py`, dix pièces) est créé
  par `create_thesis`, et pour une thèse existante par
  `manage.py admin_file_seed (--thesis | --all)`, idempotente.

| Code | Pièce | Section | Valide | Niveau de la PJ | PJ |
|---|---|---|---|---|---|
| `employment_contract` | Contrat de travail ST | CIFRE | tuteur | INTERNAL_ST | facultative |
| `cifre_agreement` | Convention CIFRE (ANRT) | CIFRE | tuteur | INTERNAL_ST | obligatoire |
| `collaboration_contract` | Contrat de collaboration ST-LIRMM | CIFRE | directeur ET tuteur | CONFIDENTIAL | obligatoire |
| `funding_proof` | Justificatif de financement | CIFRE | directeur | CONFIDENTIAL | obligatoire |
| `fsd_zrr_clearance` | Avis FSD / accès ZRR | ACADEMIC | directeur | INTERNAL_LIRMM | facultative |
| `adum_enrollment` | Inscription ADUM | ACADEMIC | directeur | INTERNAL_LIRMM | obligatoire |
| `doctoral_charter` | Charte du doctorat signée | ACADEMIC | directeur | INTERNAL_LIRMM | obligatoire |
| `training_agreement` | Convention individuelle de formation | ACADEMIC | directeur | INTERNAL_LIRMM | obligatoire |
| `training_plan` | Plan individuel de formation | ACADEMIC | directeur | INTERNAL_LIRMM | obligatoire |
| `cvec_certificate` | Attestation CVEC | ACADEMIC | directeur | INTERNAL_LIRMM | obligatoire |

## Fonctionnalités
1. Liste de contrôle par section visible, avec validateurs, statut, échéance,
   pièce jointe (via M2 : `vault.services.store` / `add_version`, ou un document
   existant de la même thèse).
2. Automate (`transitions.py`) : TODO → SUBMITTED ; SUBMITTED → TODO (retrait),
   VALIDATED (tous les rôles validateurs ont approuvé) ou REJECTED (motif) ;
   REJECTED → SUBMITTED ; VALIDATED → TODO (réouverture, motif, par le doctorant
   ou un validateur). Toute autre paire est refusée. Dossier gelé pour une thèse
   `DEFENDED` ou `CLOSED`.
3. Validation par pièce (`validator_roles`, pas le droit de lecture de la
   section), ré-authentification avec second facteur pour approuver, refuser et
   rouvrir, refus journalisés ; seule la version déposée est approuvée (409 si le
   document a changé).
4. Barre de progression sur les pièces obligatoires visibles ; passage
   automatique et atomique de la thèse à `ACTIVE` ; une thèse `ACTIVE` dont une
   pièce est rouverte reste `ACTIVE`, avec le bandeau « Dossier incomplet ».
5. Pièce jointe lisible seulement avec le droit de lecture de la section de la
   pièce (`permissions.register_document_restriction`), en plus du niveau.
6. Rappels J-14, J-3 et J+1 (en retard), en dates à Paris, une fois chacun
   (timer systemd quotidien, tâches Celery, courriel sans contenu).
   **Revérifiés à l'envoi** (branche `notifications-still-due`, `core.delivery`) :
   thèse ouverte, destinataire encore concerné (compte et adhésion actifs, rôle du
   courriel, droit de lecture de la section) et pièce encore attendue (même
   échéance ; doctorant d'une pièce `TODO` ou `REJECTED`, validateur dont le rôle
   n'a pas approuvé, jamais l'auteur du dépôt) ; sinon `SKIPPED` (migration 0006).
   Notifications d'événement : `REOPENED` due jusqu'à la revalidation,
   `DUE_PROPOSED` tant que la proposition attend ; les décisions et
   `DUE_CHANGED` restent dues. Un rappel `FAILED` repris le lendemain part si la
   pièce est toujours attendue. Une ligne restée `PENDING` avant la migration
   reste inerte (la commande ne reprend que les `FAILED`) ; tests dans
   `test_still_due.py`.
7. Pièces personnalisées : libellé en clair avec avertissement, niveau
   `CONFIDENTIAL` par défaut, validateurs qui voient la section et lisent le niveau.

## Critères d'acceptation
Tous validés à la revue de fin de M3 (2026-11-13).

- [x] La thèse ne passe `ACTIVE` que si toutes les pièces obligatoires sont
  `VALIDATED`, atomiquement avec la dernière validation, une seule fois.
  *Tests* : `test_activation.py` (tous).
- [x] Seul un rôle de `validator_roles` valide une pièce : un co-encadrant ne
  valide rien, le directeur ne valide pas une pièce du tuteur, le déposant ne
  valide ni ne refuse, une personne qui cumule les deux rôles ne valide pas seule
  le contrat de collaboration. *Tests* : `test_validation.py`
  (`test_co_supervisor_cannot_validate`, `test_only_the_item_validator_roles_approve`,
  `test_director_cannot_validate_the_tutors_item_although_they_read_it`,
  `test_the_submitter_never_validates_their_cycle`,
  `test_one_person_with_both_roles_cannot_validate_the_collaboration_contract_alone`).
- [x] Chaque changement de statut crée un `AuditEvent` (`from`, `to`).
  *Test* : `test_transitions.py::test_every_status_change_is_audited_with_from_and_to`.
- [x] Toute transition non prévue est refusée sans rien changer.
  *Test* : `test_transitions.py::test_every_pair` (20 paires).
- [x] Les pièces jointes héritent par défaut du niveau de leur pièce
  (`CONFIDENTIAL` pour une pièce personnalisée), et seulement d'un niveau que tous
  ses validateurs lisent. *Tests* : `test_submit.py::test_upload_is_stored_by_the_vault_at_the_default_level`,
  `::test_a_chosen_level_must_be_readable_by_every_validator`,
  `test_views.py::test_a_custom_item_defaults_to_confidential`.
- [x] Une pièce jointe n'est lisible qu'avec le droit de lecture de la section de
  sa pièce ; ni une autorisation explicite ni un niveau abaissé ne la contournent.
  *Tests* : `test_attachment_restriction.py`, `tests/test_security_audit_m3.py
  ::test_neither_a_grant_nor_a_lower_level_opens_an_attachment`.
- [x] Approuver, refuser et rouvrir demandent un second facteur de moins de
  10 minutes. *Tests* : `test_views.py::test_without_a_recent_second_factor_nothing_happens`,
  `tests/test_security_audit_m3.py::test_a_password_alone_does_not_allow_a_decision`.
- [x] Rappels J-14, J-3 et J+1 en dates à Paris, une fois chacun, un lien
  seulement. *Tests* : `test_reminders.py` (tous), `test_reminder_timeout.py`.

## Révision du 2026-11-14 (après la clôture : droits de réouverture et échéances)

Branche `m3-droits-echeances`, décidée après l'essai de la démo
(`docs/decisions.md`). Nouveaux modèles : `ItemReopening` (un cycle rouvert),
`DueDateProposal` (une seule en attente par pièce), `ItemNotification`
(courriel d'événement, lien seulement) ; migrations `0003` à `0005`.

- [x] Le doctorant rouvre avec les motifs `AMENDMENT`, `EXPIRED`, `OTHER`
  seulement ; un validateur, avec tous. *Tests* : `test_reopen_rules.py`
  (`test_the_student_reopens_with_three_reasons_only`,
  `test_a_validator_keeps_every_reason`), `test_due_date_views.py
  ::test_the_student_reopen_form_offers_three_reasons`.
- [x] Une réouverture notifie (lien seulement) les auteurs des validations
  annulées, sauf s'ils rouvrent eux-mêmes ; un ancien membre n'est pas notifié.
  *Tests* : `test_reopen_rules.py` (section *notifications*).
- [x] Une pièce rouverte n'affiche plus ses validations comme valides (« Validée
  le … puis rouverte le … par … — motif … ») ; un cycle refusé ou retiré liste
  ses approbations annulées. *Tests* : `test_due_date_views.py
  ::test_a_reopened_item_shows_no_valid_approval`,
  `::test_a_rejected_two_party_item_shows_the_cancelled_approval`.
- [x] Seul un validateur de la pièce modifie l'échéance d'une pièce du catalogue,
  avec second facteur récent (403 avant toute ré-authentification pour un autre
  rôle) ; le doctorant est notifié ; pièces personnalisées : le doctorant, comme
  avant. *Tests* : `test_due_dates.py`, `test_due_date_views.py
  ::test_catalogue_due_date_by_role`, `::test_custom_due_date_by_role`.
- [x] Le doctorant propose une date avec un motif de liste fermée ; un validateur
  accepte ou refuse (second facteur) ; une seule proposition en attente ;
  refusée sans validateur actif dans l'équipe. *Tests* : `test_due_dates.py`
  (sections *proposals*, *decisions*), `test_due_date_views.py`.
- [x] Échéance entre la date d'effet CIFRE et la fin prévue ; jamais effacée pour
  une pièce obligatoire du catalogue (service et contrainte en base). *Tests* :
  `test_due_dates.py::test_dates_stay_inside_the_thesis`,
  `::test_a_mandatory_catalogue_item_keeps_a_due_date`,
  `::test_the_database_refuses_it_too`.
- [x] Journal : `from` et `to` (dates ISO) pour toute modification, proposition,
  acceptation, refus, retrait, remplacement ; code du motif, jamais le
  commentaire. *Tests* : `test_due_dates.py`.
- [x] Réouvertures et propositions décidées jamais modifiées ni supprimées.
  *Tests* : `test_histories.py`.
- [x] Migration `0004` : calcul du catalogue figé dans la migration, rejouée
  depuis `0002`. *Test* : `test_due_date_migration.py`.

## Notes issues de M1
- Validation par pièce : le tuteur industriel et le directeur peuvent valider les
  pièces dont ils sont validateurs. C'est une **autorisation par pièce**, fondée
  sur `RequiredItem.validator_roles`, en plus de la matrice
  (`ADMIN_FILE_READ` / `CIFRE_FILE_READ`) : elle n'ajoute pas d'action globale de
  validation du dossier. Validation = ré-auth avec second facteur
  (`RecentAuthRequiredMixin`).
