"""Argon2 hasher with minimal cost, for the test suite only (config/settings/test.py)."""

from django.contrib.auth.hashers import Argon2PasswordHasher


class FastArgon2PasswordHasher(Argon2PasswordHasher):
    time_cost = 1
    memory_cost = 1024  # KiB
    parallelism = 1
