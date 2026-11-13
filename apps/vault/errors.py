class AuditError(Exception):
    """Programming error in an audit call, or an attempt to alter the audit log."""


class VaultIntegrityError(Exception):
    """A ciphertext, nonce, tag, header or associated data failed to authenticate.

    Always raised with the same fixed message and no chained cause: it must not
    tell which part was wrong, nor carry any byte of key or data.
    """


INTEGRITY_MESSAGE = "integrity check failed"


class VaultKeyError(Exception):
    """Base of KEK registry errors. Messages name a kek_id (not secret), never a key."""


class UnknownKekError(VaultKeyError):
    """kek_id malformed, unknown, retired, or of a provider that is not enabled."""


class KekRetirementError(VaultKeyError):
    """A KEK version cannot be retired (current, still referenced, or uncountable)."""


class KekLockTimeoutError(VaultKeyError):
    """The domain's exclusive KEK lock was not obtained in time; retry later."""


class LockOrderError(RuntimeError):
    """Programming error: a KEK lock requested after the audit-log lock (see vault.locks)."""


class StorageError(Exception):
    """A blob could not be written or read safely. Messages hold no content."""


class BlobNotFoundError(StorageError):
    """The blob a document version points to does not exist."""


class ReauthenticationRequiredError(Exception):
    """Opening this version needs a second factor less than 10 minutes old (CONFIDENTIAL).

    Raised before anything is read or decrypted; the view answers with the
    re-authentication redirect (accounts.reauth).
    """
