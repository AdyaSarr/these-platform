# M3 — Dossier administratif

## Objectif
Savoir d'un coup d'œil si toute la paperasse est faite, par qui, et ce qui manque.
La thèse passe à `ACTIVE` quand toutes les pièces obligatoires sont validées.

## Modèles
- `RequiredItem(thesis, code, label, responsible_role, mandatory, due_date, status:
  TODO|SUBMITTED|VALIDATED|REJECTED, document (FK vault, facultatif), validated_by,
  validated_at, comment)`
- Liste initiale chargée par une migration de données :
  contrat de travail ST ; convention CIFRE (ANRT) ; contrat de collaboration
  ST–LIRMM ; avis FSD / accès ZRR ; inscription ADUM ; charte du doctorat signée ;
  convention individuelle de formation ; plan individuel de formation ;
  attestation CVEC ; justificatif de financement.

## Fonctionnalités
1. Liste de contrôle avec responsable, statut, date, pièce jointe (via M2).
2. Seul le rôle responsable (ou le doctorant pour dépôt) change le statut ; la
   validation demande une ré-authentification et est journalisée.
3. Barre de progression ; passage automatique de la thèse à `ACTIVE`.
4. Rappels Celery à J-14 et J-3 pour les pièces en retard (courriel sans contenu).
5. Ajout de pièces personnalisées (ex. renouvellement de titre de séjour, sans
   stocker de détail sensible dans le libellé).

## Critères d'acceptation
- [ ] La thèse ne passe `ACTIVE` que si toutes les pièces obligatoires sont `VALIDATED`.
- [ ] Un co-encadrant ne peut pas valider une pièce dont il n'est pas responsable.
- [ ] Chaque changement de statut crée un `AuditEvent`.
- [ ] Les pièces jointes héritent par défaut du niveau `CONFIDENTIAL`.
