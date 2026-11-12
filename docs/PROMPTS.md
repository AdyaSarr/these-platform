# Prompts à donner à Claude Code, module par module

Méthode pour chaque module :

1. Ouvre une **nouvelle conversation** Claude Code (un module = une conversation).
2. Passe en **mode plan**, colle le prompt « Plan » du module, relis le plan, corrige-le.
3. Quand le plan te convient, colle le prompt « Exécution ».
4. Relis chaque diff avant de l'accepter, surtout dans `apps/vault/` et `permissions.py`.
5. Colle le prompt « Revue de fin », puis fais un commit.

Utilise `@` pour pointer les fichiers, par exemple `@docs/modules/M04-objectifs.md`.

---

## M0 — Socle

**Plan**
```
Lis @CLAUDE.md, @docs/ARCHITECTURE.md, @docs/SECURITY.md et @docs/modules/M00-socle.md.
Propose un plan détaillé pour le module M0 : arborescence, dépendances avec leur
justification, contenu de chaque fichier de settings, tests à écrire.
N'écris aucun fichier avant que je valide le plan.
```

**Exécution**
```
Le plan est validé. Implémente M0 étape par étape. Écris d'abord les tests des
critères d'acceptation, puis le code. Après chaque étape, lance les tests et
montre-moi le résultat. Ne lis jamais le fichier .env.
```

**Revue de fin**
```
Vérifie un par un les critères d'acceptation de @docs/modules/M00-socle.md et dis
lesquels sont remplis, avec la preuve (test ou commande). Lance pytest, ruff,
bandit et `manage.py check --deploy --settings=config.settings.prod`.
Liste ce qui reste. Coche M0 dans @docs/ROADMAP.md seulement si tout est vert.
```

---

## M1 — Comptes, rôles, invitations, MFA

**Plan**
```
Lis @CLAUDE.md, @docs/ARCHITECTURE.md (matrice des droits), @docs/SECURITY.md et
@docs/modules/M01-comptes.md. Propose : modèles, migrations, URLs, vues,
la conception de permissions.py (fonction can() + mixins), la gestion du jeton
d'invitation et le choix de bibliothèque MFA (compare django-otp et
django-allauth MFA, avec avantages et limites). Liste les tests, en particulier
un test autorisé et un test refusé par rôle et par action.
N'écris rien avant validation.
```

**Exécution**
```
Plan validé. Implémente M1. Commence par permissions.py et ses tests, puis les
modèles, puis les invitations, puis le MFA obligatoire. Lance les tests après
chaque étape.
```

**Revue de fin**
```
Joue le rôle d'un auditeur de sécurité : cherche dans le code de M1 tout moyen
d'accéder à une thèse sans Membership active, de réutiliser un jeton, de
contourner le second facteur ou d'élever son rôle. Pour chaque piste, écris un
test qui le prouve ou le réfute. Puis vérifie les critères d'acceptation.
```

---

## M2 — Coffre chiffré et journal d'audit

**Plan**
```
Lis @CLAUDE.md, @docs/SECURITY.md et @docs/modules/M02-coffre.md. Propose la
conception de apps/vault/ : format exact des données associées, stockage des
blobs, dérivation des KEK par HKDF en dev, interface prévue pour Vault en prod,
rotation des KEK, structure de la chaîne d'audit et trigger PostgreSQL qui
interdit UPDATE et DELETE. Utilise uniquement les API de haut niveau de
cryptography ou PyNaCl ; ne conçois aucune primitive.
Explique chaque choix cryptographique en une phrase. N'écris rien avant validation.
```

**Exécution**
```
Plan validé. Implémente M2. crypto.py doit rester court et ne contenir que des
appels aux bibliothèques. Écris d'abord les tests d'altération (blob, nonce,
données associées, niveau) puis le code.
```

**Revue de fin**
```
Relis apps/vault/ comme un cryptographe : réutilisation de nonce possible ?
données associées complètes ? DEK jamais journalisée ni affichée ? comparaison de
hachés en temps constant ? Corrige, ajoute les tests, puis vérifie les critères
d'acceptation de M02.
```

---

## M3 à M9 — même schéma

Remplace `MXX` et le nom du fichier.

**Plan**
```
Lis @CLAUDE.md, @docs/ARCHITECTURE.md et @docs/modules/MXX-....md. Propose
modèles, migrations, URLs, vues htmx, services, tâches Celery éventuelles et
tests. Réutilise apps/accounts/permissions.py pour les droits et
apps/vault/services.py pour tout fichier ou texte sensible et pour le journal
d'audit. N'écris rien avant validation.
```

**Exécution**
```
Plan validé. Implémente MXX en commençant par les tests des critères
d'acceptation. Lance les tests après chaque étape.
```

**Revue de fin**
```
Vérifie un par un les critères d'acceptation de @docs/modules/MXX-....md avec
preuve. Vérifie que chaque vue du module exige une permission et que chaque
contenu sensible passe par le coffre. Lance pytest, ruff, bandit. Coche MXX
dans @docs/ROADMAP.md si tout est vert.
```

---

## M10 — Bout en bout et post-quantique

```
Lis @docs/SECURITY.md et @docs/modules/M10-e2e-pq.md. Rédige d'abord un document
de conception docs/design/M10.md (modèle de menace, flux de clés, cas de perte de
phrase de passe, choix de bibliothèques et leur statut d'audit). N'écris aucun
code : je le ferai relire par mes encadrants avant.
```

---

## Prompts utiles à tout moment

- `Explique-moi ce que fait ce fichier comme à un cryptographe qui découvre Django.`
- `Lance tous les tests et explique chaque échec avant de le corriger.`
- `Mets à jour CLAUDE.md avec la convention que nous venons de décider.`
- `Propose un message de commit pour ces changements.`
