#!/usr/bin/env bash
# manage.py check --deploy with the production settings, from a development machine
# (docs/DEPLOYMENT.md). Run by the developer only: it reads .env.
#
#   scripts/check_deploy.sh [check arguments...]    # e.g. --database default
#
# The variables of .env are loaded, then replaced by fictitious production values
# (the dev ones would be refused by prod.py: http:// site, local hosts, no SMTP),
# and the vault directory is a temporary one, removed at the end. On the server,
# run the check with its real environment instead.
#
# Everything happens in ONE subshell ( ... ): no variable, option or trap outlives
# the script, even if it is sourced by mistake (bash or zsh). Hence no statement
# outside the parentheses, and no `exit` that could close the caller's shell.
#
# Exit code: the one of check --deploy (non-zero from the first WARNING), 1 without
# .env, 2 if the temporary directory cannot be created.

(
  repo="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd -P)" || exit 2
  cd "$repo" || exit 2
  if [ ! -f .env ]; then
    echo "check_deploy: pas de fichier .env dans $repo (modèle : .env.example)." >&2
    exit 1
  fi

  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a

  vault="$(mktemp -d)" || exit 2
  trap 'rm -rf "$vault"' EXIT
  trap 'exit 130' INT TERM

  export DJANGO_SETTINGS_MODULE=config.settings.prod
  export DJANGO_SITE_URL=https://these.example.org
  export DJANGO_ALLOWED_HOSTS=these.example.org
  export DJANGO_CSRF_TRUSTED_ORIGINS=https://these.example.org
  export EMAIL_URL=smtp+tls://demo:demo@smtp.example.org:587  # pragma: allowlist secret
  export DEFAULT_FROM_EMAIL=noreply@these.example.org
  export VAULT_STORAGE_DIR="$vault"

  uv run python manage.py check --deploy --settings=config.settings.prod --fail-level WARNING "$@"
  code=$?
  rm -rf "$vault"
  exit "$code"
)
