# Bibliothèques vendorisées

Servies localement (pas de CDN). L'empreinte de référence est dans
`docs/decisions.md` ; `tests/test_repo_hygiene.py` la recalcule à chaque exécution.

| Fichier | Version | Source |
|---|---|---|
| `htmx/htmx.min.js` | htmx 2.0.11 | `package/dist/htmx.min.js` de https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz |

## Mettre à jour htmx

1. Lire la version `latest` sur https://registry.npmjs.org/htmx.org (dist-tags).
2. Télécharger l'archive `.tgz` et vérifier son empreinte sha512 contre le champ
   `dist.integrity` du registre.
3. Extraire `package/dist/htmx.min.js` ici.
4. Calculer l'empreinte :
   `python -c "import base64,hashlib;print('sha384-'+base64.b64encode(hashlib.sha384(open('static/vendor/htmx/htmx.min.js','rb').read()).digest()).decode())"`
5. Ajouter une ligne dans `docs/decisions.md` (version, URL, SHA-384) et mettre à
   jour ce tableau.
