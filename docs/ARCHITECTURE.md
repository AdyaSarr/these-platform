# Architecture

## Vue d'ensemble

```
Navigateur (htmx, WebAuthn, chiffrement E2E au M10)
        │  TLS 1.3 (hybride X25519MLKEM768 en production)
        ▼
Proxy inverse (Caddy ou Nginx)
        ▼
Django (apps/) ──► PostgreSQL (champs sensibles chiffrés)
   │   │   └────► Stockage fichiers (blobs chiffrés AEAD)
   │   └────────► Gestionnaire de clés (KEK : variable d'env. en dev, Vault/OpenBao en prod)
   └────────────► Celery + Redis (rappels, import des deadlines)
Sauvegardes chiffrées (restic) — journal d'audit chaîné
```

## Arborescence cible

```
these-platform/
├── CLAUDE.md
├── pyproject.toml
├── docker-compose.yml
├── .env.example
├── config/                 # projet Django (settings/base.py, dev.py, prod.py, urls.py, celery.py)
├── apps/
│   ├── core/               # M0 : modèles abstraits, page d'accueil, santé
│   ├── accounts/           # M1 : utilisateurs, thèses, rôles, invitations, MFA
│   ├── vault/              # M2 : documents chiffrés, clés, journal d'audit
│   ├── admin_file/         # M3 : dossier administratif
│   ├── objectives/         # M4 : objectifs et indicateurs
│   ├── meetings/           # M5 : réunions et présentations
│   ├── conferences/        # M6 : conférences et deadlines
│   ├── travel/             # M7 : déplacements et temps ST / LIRMM
│   ├── reports/            # M8 : rapports CSI et ANRT
│   └── portfolio/          # M9 : formations, publications, portfolio
├── templates/
├── static/
├── tests/                  # tests transverses (permissions, sécurité)
└── docs/
```

## Modèle de domaine (noyau)

- **User** : utilisateur personnalisé (courriel comme identifiant).
- **Organization** : ST, LIRMM, Université de Montpellier, externe.
- **Thesis** : titre, date d'effet CIFRE, date de 1re inscription, date de fin prévue,
  statut (`SETUP`, `ACTIVE`, `DEFENDED`, `CLOSED`).
- **Membership** : (user, thesis, role, organization, actif du / au).
  Rôles : `PHD_STUDENT`, `DIRECTOR`, `CO_SUPERVISOR`, `INDUSTRIAL_TUTOR`,
  `CSI_MEMBER`, `GUEST`.
- **ThesisYear** : année n de thèse, double bornage (année CIFRE, année universitaire).
- **Deadline** : échéance générique (type, date, responsable, objet lié, rappels).
- **Document** (M2) : métadonnées en clair (titre, niveau, auteur) + contenu chiffré.
- **AuditEvent** (M2) : journal en ajout seul, chaîné par hachage.

Chaque objet métier appartient à une `Thesis` : la plateforme est mono-thèse au départ
mais le modèle permet d'en accueillir plusieurs (autres doctorants CIFRE plus tard).

## Matrice des droits (référence pour `permissions.py`)

| Rôle | Objectifs | Réunions | Documents | Admin./RH | Valide |
|---|---|---|---|---|---|
| PHD_STUDENT | écrire | écrire | écrire | écrire | — |
| DIRECTOR | commenter | écrire | lire, commenter | lire | objectifs, CSI, ANRT |
| CO_SUPERVISOR | commenter | écrire | lire, commenter | — | — |
| INDUSTRIAL_TUTOR | commenter | écrire | lire, commenter | partie CIFRE | ANRT, publications |
| CSI_MEMBER | dossier CSI | — | rapport CSI | — | avis CSI |
| GUEST | — | une réunion | projet partagé | — | — |

Accès à un document = rôle autorisé **ET** niveau compatible :

| Niveau | Rôles autorisés |
|---|---|
| PUBLIC | tous les membres actifs |
| INTERNAL_LIRMM | doctorant, directeur, co-encadrants |
| INTERNAL_ST | doctorant, tuteur industriel |
| CONFIDENTIAL | doctorant, directeur, tuteur industriel (+ liste explicite) |
