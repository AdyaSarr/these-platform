{% load i18n %}{% translate "Export of your data from the thesis platform" %}
{% blocktranslate with date=at|date:"j F Y" %}Made on {{ date }}.{% endblocktranslate %}

{% translate "manifest.json: every file of this archive with its size and SHA-256 digest (format these-export/1)." %}
{% translate "documents.json and documents/: your documents, decrypted, by number; the names of the files are built by the platform." %}
{% translate "these.json, equipe.json, objectifs.json, reunions.json, dossier-administratif.json, conferences.json, deplacements.json: your data, texts decrypted." %}
{% translate "journal/mes-evenements.json: your own lines of the audit log." %}

{% translate "Left out: second factor secrets, links tokens, passwords, the addresses of the other members, the lines of the audit log of the others, and INTERNAL_ST without an agreement of ST (their number is in manifest.json)." %}
{% translate "This archive holds your documents in clear: keep it on an encrypted disk." %}
