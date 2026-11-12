# M9 — Portfolio, formations, publications

## Objectif
Préparer ce qu'ADUM demandera : heures de formation, intégrité scientifique,
publications, enseignement, projet professionnel. La plateforme prépare, ADUM
enregistre.

## Modèles
- `Training(thesis, title, provider, category: ED|SCIENTIFIC|TRANSVERSAL|
  INTEGRITY|OTHER, hours, date, certificate (Document), validated_in_adum: bool)`
- `Publication(thesis, title, authors, venue, year, status, doi, hal_id, eprint_id,
  st_approval_status, bibtex, file (Document))`
- `TeachingActivity(thesis, course, hours, year)`
- `CareerNote(thesis, date, text)` (niveau `INTERNAL`)

## Fonctionnalités
1. Compteur vers 100 h avec répartition par catégorie ; alerte si la formation à
   l'intégrité n'est pas faite avant la 3e année.
2. Publications : import BibTeX, export BibTeX et liste prête pour HAL-LIRMM ;
   une publication ne passe `SUBMITTED` que si `st_approval_status = APPROVED`.
3. Export « dossier de soutenance » (liste formations + publications) en PDF.

## Critères d'acceptation
- [ ] Une publication sans accord ST ne peut pas passer `SUBMITTED` (test).
- [ ] L'import BibTeX rejette les entrées malformées sans planter (test).
- [ ] Les totaux d'heures par catégorie sont exacts (test).
