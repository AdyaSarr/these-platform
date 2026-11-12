import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_asgi_application()

# After setup: refuse to serve while MFA secrets would be stored in clear.
from apps.accounts.guards import require_mfa_encryption  # noqa: E402

require_mfa_encryption()
