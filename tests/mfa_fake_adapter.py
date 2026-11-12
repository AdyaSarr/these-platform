"""Test-only MFA adapter with a reversible, non-identity encrypt(): lets a test show
that the production guard checks behaviour and passes once encryption is real.
Not cryptography (and never used outside tests)."""

from allauth.mfa.adapter import DefaultMFAAdapter


class ReversibleMFAAdapter(DefaultMFAAdapter):
    def encrypt(self, text: str) -> str:
        return "test-only:" + text[::-1]

    def decrypt(self, encrypted_text: str) -> str:
        return encrypted_text.removeprefix("test-only:")[::-1]
