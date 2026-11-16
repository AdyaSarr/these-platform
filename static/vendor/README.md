# Bibliothèques vendorisées

Servies localement (pas de CDN). L'empreinte de référence est dans
`docs/decisions.md` ; `tests/test_repo_hygiene.py` la recalcule à chaque exécution.

| Fichier | Version | Source |
|---|---|---|
| `htmx/htmx.min.js` | htmx 2.0.11 | `package/dist/htmx.min.js` de https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz |
| `inter/inter-latin-wght-{normal,italic}.woff2`, `inter/OFL.txt` | Inter (`@fontsource-variable/inter` 5.3.0), OFL-1.1 | `package/files/` et `package/LICENSE` de https://registry.npmjs.org/@fontsource-variable/inter/-/inter-5.3.0.tgz |
| `lucide/*.svg`, `lucide/LICENSE` | `lucide-static` 1.66.0, ISC | `package/icons/<nom>.svg` et `package/LICENSE` de https://registry.npmjs.org/lucide-static/-/lucide-static-1.66.0.tgz |

`SHA384SUMS` liste chaque fichier de ce dossier (sauf ce README) avec son SHA-384 ;
l'empreinte du manifeste lui-même est dans `docs/decisions.md`.

## Ajouter ou mettre à jour une police ou une icône

1. Prendre une version publiée depuis au moins deux semaines (`time` du registre).
2. Télécharger l'archive `.tgz`, vérifier son sha512 contre `dist.integrity`.
3. Copier les fichiers **sans les modifier** (une icône : `package/icons/<nom>.svg` ;
   l'ajouter aussi à `ICON_NAMES` dans `apps/ui/icons.py`).
4. Régénérer le manifeste depuis ce dossier :
   `find . -type f ! -name README.md ! -name SHA384SUMS ! -name .DS_Store | sed 's|^\./||' | sort | while read f; do echo "sha384-$(openssl dgst -sha384 -binary "$f" | base64)  $f"; done > SHA384SUMS`
5. Ajouter une ligne dans `docs/decisions.md` avec la nouvelle empreinte de `SHA384SUMS`
   (`openssl dgst -sha384 -binary SHA384SUMS | base64`).

## Mettre à jour htmx

1. Lire la version `latest` sur https://registry.npmjs.org/htmx.org (dist-tags).
2. Télécharger l'archive `.tgz` et vérifier son empreinte sha512 contre le champ
   `dist.integrity` du registre.
3. Extraire `package/dist/htmx.min.js` ici.
4. Calculer l'empreinte :
   `python -c "import base64,hashlib;print('sha384-'+base64.b64encode(hashlib.sha384(open('static/vendor/htmx/htmx.min.js','rb').read()).digest()).decode())"`
5. Ajouter une ligne dans `docs/decisions.md` (version, URL, SHA-384) et mettre à
   jour ce tableau.
6. Régénérer `SHA384SUMS` et noter sa nouvelle empreinte (étapes 4 et 5 de la
   section précédente).
