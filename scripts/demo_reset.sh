#!/usr/bin/env bash
# Recreate the demonstration database from scratch, then run demo_seed.
#
#   scripts/demo_reset.sh [--yes]
#
# Deletes the database of config.settings.demo (name ending with _demo), then
# var/vault-demo and var/demo-audit, after showing them and asking for a typed
# "oui" (unless --yes). This script never reads the environment file itself: it
# relies on the variables already set, and on Django's settings, as every
# manage.py command does. The links printed at the end are shown once, here only.
set -euo pipefail

VAULT_REL="var/vault-demo"
AUDIT_REL="var/demo-audit"

fail() {
  printf 'demo_reset : %s\n' "$1" >&2
  exit 1
}

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=1 ;;
    *)
      printf 'usage : %s [--yes]\n' "$0" >&2
      exit 64
      ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO"
export DJANGO_SETTINGS_MODULE=config.settings.demo

manage() {
  uv run python manage.py "$@"
}

# Absolute path of a direct child of var/ in the repository; anything else refused.
under_var() {
  local rel="$1" child parent abs
  [[ -n "$rel" ]] || fail "chemin vide refusé"
  case "$rel" in
    /* | *..* | *//*) fail "chemin refusé : $rel" ;;
  esac
  [[ "$rel" == var/?* ]] || fail "chemin refusé : $rel (hors de var/)"
  child="${rel#var/}"
  case "$child" in
    */* | .*) fail "chemin refusé : $rel" ;;
  esac
  mkdir -p "$REPO/var"
  parent="$(cd "$REPO/var" && pwd -P)"
  [[ "$parent" == "$REPO/var" ]] || fail "var/ n'est pas dans le dépôt ($parent)"
  abs="$parent/$child"
  [[ ! -L "$abs" ]] || fail "$abs est un lien symbolique : refusé"
  printf '%s' "$abs"
}

VAULT_DIR="$(under_var "$VAULT_REL")"
AUDIT_DIR="$(under_var "$AUDIT_REL")"

DESCRIBE="$(manage demo_db describe)"
DB_NAME="$(printf '%s\n' "$DESCRIBE" | sed -n 's/^database=//p')"
DJANGO_VAULT="$(printf '%s\n' "$DESCRIBE" | sed -n 's/^vault=//p')"
[[ "$DB_NAME" =~ ^[a-z][a-z0-9_]*_demo$ ]] ||
  fail "le nom de la base doit se terminer par _demo (reçu : '$DB_NAME') : rien n'est supprimé"
[[ "$DJANGO_VAULT" == "$VAULT_DIR" ]] ||
  fail "VAULT_STORAGE_DIR de Django ($DJANGO_VAULT) n'est pas $VAULT_DIR : rien n'est supprimé"

printf 'Seront supprimés puis recréés :\n'
printf '  base de données  %s (toutes ses données)\n' "$DB_NAME"
printf '  répertoire       %s\n' "$VAULT_DIR"
printf '  répertoire       %s\n' "$AUDIT_DIR"
if [[ "$ASSUME_YES" -ne 1 ]]; then
  printf 'Tapez « oui » pour confirmer : '
  answer=""
  IFS= read -r answer || true
  [[ "$answer" == "oui" ]] || fail "non confirmé : rien n'est supprimé"
fi

manage demo_db drop
rm -rf -- "${VAULT_DIR:?}"
rm -rf -- "${AUDIT_DIR:?}"
manage demo_db create
manage migrate --noinput
SEED_OUTPUT="$(manage demo_seed)"

# External copy of the audit chain's end, then a full verification against it.
mkdir -m 700 "$AUDIT_DIR"
manage audit_checkpoint --export-public-key |
  sed -n 's/^Ed25519 public key: //p' >"$AUDIT_DIR/public-key.txt"
[[ -s "$AUDIT_DIR/public-key.txt" ]] || fail "clé publique des points de contrôle introuvable"
manage audit_checkpoint --export "$AUDIT_DIR/checkpoints.txt" >/dev/null
manage audit_verify --public-key "$AUDIT_DIR/public-key.txt" \
  --checkpoints "$AUDIT_DIR/checkpoints.txt"

printf '\n%s\n' "$SEED_OUTPUT"
