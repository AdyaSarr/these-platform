"""Document pages (mounted under /t/<thesis_id>/documents/).

Thin views: every one is guarded at thesis level by ThesisPermissionRequiredMixin
(tests/test_thesis_routes.py), and the document-level decision is taken by
vault.services, which audits refusals. Check order: right (403), then
re-authentication, then the service (docs/decisions.md).
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseBase, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import Membership, User
from apps.accounts.permissions import (
    Action,
    ThesisPermissionRequiredMixin,
    can,
)
from apps.accounts.reauth import RecentAuthRequiredMixin, authenticated_recently, reauth_response
from apps.core.confidentiality import ConfidentialityLevel

from . import services
from .errors import ReauthenticationRequiredError, StorageError, UnknownKekError
from .errors import VaultIntegrityError as IntegrityFailure
from .forms import GrantForm, LevelForm, NewVersionForm, UploadForm
from .models import Document, DocumentAccessGrant


def _user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):  # the thesis guard already refused anonymous users
        raise PermissionDenied
    return user


def _upload_error(request: HttpRequest, form: Any) -> None:
    if getattr(request, "upload_too_large", False):
        form.add_error("file", _("The file is larger than %(limit)d MB.") % {"limit": 25})


class _DocumentMixin(ThesisPermissionRequiredMixin):
    """Thesis guard first. Views needing re-authentication list RecentAuthRequiredMixin
    AFTER this mixin and BEFORE View (View.dispatch does not call super())."""

    required_action = Action.VIEW_THESIS
    request: HttpRequest

    def _url(self, name: str, **kwargs: Any) -> str:
        return reverse(f"vault:{name}", kwargs={"thesis_id": self.thesis.pk, **kwargs})

    def _document(self, document_id: Any, operation: str) -> Document:
        return services.document_for(
            _user(self.request), self.thesis, document_id, operation=operation
        )


class DocumentListView(_DocumentMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = _user(request)
        documents = services.readable_documents(user, self.thesis)  # also the card's count
        return render(
            request,
            "vault/document_list.html",
            {
                "thesis": self.thesis,
                "documents": documents,
                "can_upload": can(user, Action.DOCUMENT_WRITE, self.thesis),
            },
        )


class DocumentDetailView(_DocumentMixin, View):
    def get(
        self, request: HttpRequest, *args: Any, document_id: Any, **kwargs: Any
    ) -> HttpResponse:
        user = _user(request)
        document = self._document(document_id, "detail")
        manages = can(user, Action.DOCUMENT_WRITE, document)
        grants = document.grants.filter(revoked_at__isnull=True).select_related("user")
        members = (
            Membership.objects.active()
            .filter(thesis=self.thesis)
            .exclude(user=user)
            .select_related("user")
        )
        return render(
            request,
            "vault/document_detail.html",
            {
                "thesis": self.thesis,
                "document": document,
                "versions": document.versions.order_by("-version"),
                "manages": manages,
                "can_grant": can(user, Action.DOCUMENT_GRANT, document)
                and document.confidentiality == ConfidentialityLevel.CONFIDENTIAL,
                "grants": grants,
                "members": members,
                "level_form": LevelForm(user=user, thesis=self.thesis) if manages else None,
                "version_form": NewVersionForm() if manages else None,
            },
        )


class UploadView(_DocumentMixin, View):
    required_action = Action.DOCUMENT_WRITE

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = UploadForm(user=_user(request), thesis=self.thesis)
        return render(request, "vault/upload.html", {"thesis": self.thesis, "form": form})

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        user = _user(request)
        form = UploadForm(request.POST, request.FILES, user=user, thesis=self.thesis)
        _upload_error(request, form)
        if form.is_valid():
            try:
                document = services.store(
                    thesis=self.thesis,
                    upload=form.cleaned_data["file"],
                    title=form.cleaned_data["title"],
                    level=ConfidentialityLevel(form.cleaned_data["level"]),
                    user=user,
                )
            except ValidationError as exc:
                form.add_error("file" if exc.code != "title" else "title", exc)
            else:
                messages.success(request, _("Document stored."))
                return HttpResponseRedirect(self._url("document", document_id=document.pk))
        return render(request, "vault/upload.html", {"thesis": self.thesis, "form": form})


class NewVersionView(_DocumentMixin, View):
    required_action = Action.DOCUMENT_WRITE

    def post(
        self, request: HttpRequest, *args: Any, document_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        document = self._document(document_id, "add_version")
        form = NewVersionForm(request.POST, request.FILES)
        _upload_error(request, form)
        stored = False
        if form.is_valid():
            try:
                services.add_version(document=document, upload=form.cleaned_data["file"], user=user)
                stored = True
            except ValidationError as exc:
                form.add_error("file", exc)
        if stored:
            messages.success(request, _("New version stored."))
        for error in form.errors.get("file", []):
            messages.error(request, str(error))
        return HttpResponseRedirect(self._url("document", document_id=document.pk))


class DownloadView(_DocumentMixin, View):
    @sensitive_variables()  # the decrypted content is a local of this frame
    def get(
        self, request: HttpRequest, *args: Any, version_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        try:
            opened = services.open_version(
                version_id,
                _user(request),
                thesis=self.thesis,
                recent_mfa=authenticated_recently(request, mfa=True),
            )
        except ReauthenticationRequiredError:
            return reauth_response(request, mfa=True)
        except (IntegrityFailure, UnknownKekError, StorageError):
            # Already audited by the service; no detail for the browser.
            return render(request, "vault/unavailable.html", {"thesis": self.thesis}, status=503)
        response = HttpResponse(opened.data, content_type="application/octet-stream")
        response["Content-Disposition"] = content_disposition_header(
            as_attachment=True, filename=opened.download_name
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "no-store, private"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Referrer-Policy"] = "no-referrer"
        return response


class ChangeLevelView(_DocumentMixin, RecentAuthRequiredMixin, View):
    required_action = Action.DOCUMENT_WRITE

    def post(
        self, request: HttpRequest, *args: Any, document_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        document = self._document(document_id, "change_level")
        form = LevelForm(request.POST, user=user, thesis=self.thesis)
        if form.is_valid():
            try:
                services.change_level(
                    document=document,
                    level=ConfidentialityLevel(form.cleaned_data["level"]),
                    user=user,
                )
                messages.success(request, _("Confidentiality level changed."))
            except ValueError:
                messages.info(request, _("The document already has this level."))
        return HttpResponseRedirect(self._url("document", document_id=document.pk))


class GrantView(_DocumentMixin, RecentAuthRequiredMixin, View):
    required_action = Action.DOCUMENT_GRANT

    def post(
        self, request: HttpRequest, *args: Any, document_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        document = self._document(document_id, "grant")
        form = GrantForm(request.POST)
        grantee = (
            User.objects.filter(pk=form.cleaned_data["grantee"]).first()
            if form.is_valid()
            else None
        )
        if grantee is None:
            raise PermissionDenied
        try:
            services.grant_access(document=document, grantee=grantee, user=user)
            messages.success(request, _("Access given."))
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        return HttpResponseRedirect(self._url("document", document_id=document.pk))


class RevokeGrantView(_DocumentMixin, RecentAuthRequiredMixin, View):
    required_action = Action.DOCUMENT_GRANT

    def post(
        self, request: HttpRequest, *args: Any, document_id: Any, grant_id: Any, **kwargs: Any
    ) -> HttpResponseBase:
        user = _user(request)
        document = self._document(document_id, "revoke")
        grant = DocumentAccessGrant.objects.filter(pk=grant_id, document=document).first()
        if grant is None:
            raise PermissionDenied
        services.revoke_access(grant=grant, user=user)
        messages.success(request, _("Access revoked."))
        return HttpResponseRedirect(self._url("document", document_id=document.pk))
