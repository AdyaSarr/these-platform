from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    """Abstract base of every business model.

    ``created_by`` is PROTECTed: a user who authored objects cannot be deleted,
    only deactivated (docs/decisions.md). ``null`` covers objects created by the
    system (e.g. imported deadlines).
    """

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
        verbose_name=_("created by"),
    )

    class Meta:
        abstract = True
