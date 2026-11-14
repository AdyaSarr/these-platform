# Notes d'interface

Remarques relevées en testant la plateforme dans un vrai navigateur, à traiter
plus tard. Cocher une note quand elle est faite.

## Tour visuel du 2026-11-14

- [ ] **Dossier admin** : une pièce dont le validateur requis n'est pas encore
      dans l'équipe devrait l'indiquer (« Aucun tuteur industriel dans l'équipe —
      invitez-le »).
- [ ] **Dossier admin** : distinguer « en retard » (à déposer) de « en attente de
      validation depuis X jours » (déposée).
- [ ] **Tableau de bord** : espace insécable avant « % » ; seul « Ouvrir le
      dossier » doit être un lien, pas toute la phrase.
- [ ] **Échec CSRF** : la page affichée est la page brute de Django. Prévoir une
      page à nous (`CSRF_FAILURE_VIEW`) qui dit simplement « Votre navigateur
      bloque les cookies de ce site ou la page a expiré : rechargez-la », sans
      détail technique.
- Graphique objectifs : la dernière étiquette de l'axe est coupée (« nov. 20: ») ; élargir le viewBox ou les marges.
- Dossier admin : le bouton « Rouvrir » prend toute la largeur et le formulaire d'échéance le chevauche ; aligner les actions sur une ligne.