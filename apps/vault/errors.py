class AuditError(Exception):
    """Programming error in an audit call, or an attempt to alter the audit log."""


class VaultIntegrityError(Exception):
    """A ciphertext, nonce, tag, header or associated data failed to authenticate.

    Always raised with the same fixed message and no chained cause: it must not
    tell which part was wrong, nor carry any byte of key or data.
    """


INTEGRITY_MESSAGE = "integrity check failed"
