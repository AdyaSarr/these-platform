"""Test-only MFA adapters that the production guard must REFUSE (M2): the vault's
MFAAdapter is the only accepted one. Not cryptography (never used outside tests)."""

from allauth.mfa.adapter import DefaultMFAAdapter

from apps.accounts.adapters import MFAAdapter


class IdentityMFAAdapter(DefaultMFAAdapter):
    def encrypt(self, text: str) -> str:
        return text

    def decrypt(self, encrypted_text: str) -> str:
        return encrypted_text


class ReversibleMFAAdapter(DefaultMFAAdapter):
    """Reversible and not the identity: a behaviour check alone would accept it."""

    def encrypt(self, text: str) -> str:
        return "test-only:" + text[::-1]

    def decrypt(self, encrypted_text: str) -> str:
        return encrypted_text.removeprefix("test-only:")[::-1]


class OverridingMFAAdapter(MFAAdapter):
    """A subclass of the right class that bypasses the vault."""

    def encrypt(self, text: str) -> str:
        return "test-only:" + text

    def decrypt(self, encrypted_text: str) -> str:
        return encrypted_text.removeprefix("test-only:")
