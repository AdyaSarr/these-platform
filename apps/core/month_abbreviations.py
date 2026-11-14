"""French month abbreviations: Django's own, completed in our catalogue.

Django's localized format "N" (MONTHS_AP, context "abbrev. month") gives in French
« jan. » and « fév. »; the usual French abbreviations are « janv. » and « févr. »
(the other months already match: mars, avr., mai, juin, juil., août, sept., oct.,
nov., déc.). Our catalogue (locale/fr) overrides these two entries; they are
referenced here so that `makemessages` keeps them. Never called at run time.
"""

from django.utils.translation import pgettext_lazy

OVERRIDDEN = (
    pgettext_lazy("abbrev. month", "Jan."),
    pgettext_lazy("abbrev. month", "Feb."),
)
