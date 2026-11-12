# M8 — Rapports CSI et ANRT

## Objectif
Générer des brouillons de rapport CSI et de rapport annuel ANRT à partir des données
de la plateforme, à relire et compléter, jamais envoyés automatiquement.

## Fonctionnalités
1. Sélection de la période (année universitaire pour le CSI, année CIFRE pour l'ANRT).
2. Agrégation : objectifs et taux d'atteinte, réunions clés, décisions,
   publications et soumissions, formations et heures, séjours ST/LIRMM.
3. Génération via gabarits Jinja → LaTeX → PDF (`latexmk` dans un conteneur, sans
   accès réseau, avec échappement strict de toutes les valeurs injectées),
   et export `.docx` (python-docx).
4. Circuit de validation : brouillon → relu par le doctorant → validé par le
   directeur (et le tuteur ST pour l'ANRT), chaque validation signée et journalisée.
5. Le rapport final est stocké dans le coffre (M2) au niveau `CONFIDENTIAL`.

## Critères d'acceptation
- [ ] Une valeur contenant `\input{/etc/passwd}` ou `\write18` est neutralisée
      (test d'injection LaTeX).
- [ ] La compilation se fait sans `shell-escape` et avec un délai maximal.
- [ ] Le rapport ANRT exige la validation de trois rôles avant l'état `FINAL`.

## Notes issues de M1
- Droits CSI à ajouter à la matrice (`apps/accounts/permissions.py`) : le
  **doctorant écrit le dossier CSI** ; **doctorant et directeur lisent le rapport et
  l'avis du CSI**. En M1, seules les actions du `CSI_MEMBER` existent
  (`CSI_FILE_READ`, `CSI_REPORT_READ`, `CSI_OPINION_WRITE`) ; M8 ajoute
  l'écriture du dossier et étend la lecture, avec les deux transcriptions
  (code et tests) mises à jour ensemble.
