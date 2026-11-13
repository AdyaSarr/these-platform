from django.apps import AppConfig


class AdminFileConfig(AppConfig):
    name = "apps.admin_file"
    label = "admin_file"
    verbose_name = "Administrative file"

    def ready(self) -> None:
        from apps.accounts.permissions import register_document_restriction

        from .access import attachment_restriction

        # Attachments are closed to roles without the section's read right.
        register_document_restriction(attachment_restriction)
