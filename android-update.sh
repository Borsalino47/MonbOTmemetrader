#!/usr/bin/env bash
# CRYPTO PULSE AI — mise à jour après un `git pull`.
#
#   ./android-update.sh
#
# Ne touche JAMAIS à vos données : .env et data/ ne sont pas suivis par Git, et
# la migration de schéma est additive uniquement — elle ajoute des colonnes et
# refuse toute opération destructive plutôt que de la deviner.
#
# Ne reconstruit que ce qui a changé. L'interface n'est reconstruite que si une
# source est plus récente que le bundle : reconstruire à chaque fois coûtait
# une minute pour un résultat identique.

set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
PY="$VENV/bin/python"

say()  { printf '\n\033[36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$1"; }

if [ ! -x "$PY" ]; then
  echo "Environnement absent. Lancez d'abord ./android-install.sh" >&2
  exit 1
fi

# ------------------------------------------------------------- sauvegarde ---
# Une seconde, avant toute opération. La mise à jour n'y touche pas, mais une
# base perdue ne se reconstruit pas : c'est le seul contenu irremplaçable ici.
DB="${CP_DB_URL##*sqlite:///}"
[ "$DB" = "${CP_DB_URL:-}" ] && DB="data/cryptopulse.db"
if [ -f "$DB" ]; then
  say "Sauvegarde de la base"
  cp "$DB" "$DB.backup"
  ok "$DB.backup"
fi

# ---------------------------------------------------------------- python ----
say "Dépendances Python"
"$VENV/bin/pip" install -q -e "." && ok "à jour"

# ---------------------------------------------------------------- icônes ----
if [ ! -f frontend/public/icons/icon-512.png ]; then
  say "Icônes manquantes"
  "$PY" scripts/make_icons.py >/dev/null && ok "régénérées"
fi

# -------------------------------------------------------------- interface ---
NEEDS_BUILD=0
[ ! -f frontend/dist/index.html ] && NEEDS_BUILD=1
if [ -f frontend/dist/index.html ] && [ -n "$(find frontend/src frontend/public frontend/index.html -newer frontend/dist/index.html 2>/dev/null | head -1)" ]; then
  NEEDS_BUILD=1
fi

if [ "$NEEDS_BUILD" = "1" ]; then
  if command -v npm >/dev/null 2>&1; then
    say "Reconstruction de l'interface"
    (cd frontend && npm install --silent && npm run build)
    ok "frontend/dist à jour"
  else
    warn "npm absent : l'interface ne peut pas être reconstruite."
  fi
else
  ok "interface déjà à jour, rien à reconstruire"
fi

# ---------------------------------------------------------------- schéma ----
# Additive uniquement. Affiche les colonnes ajoutées plutôt que de les ajouter
# en silence : voir un `column_added` au premier démarrage est normal, ne pas
# savoir ce qui a changé ne l'est pas.
say "Schéma de la base"
"$PY" - <<'PYEOF'
from cryptopulse.config.settings import get_settings
from cryptopulse.database.migrate import ensure_schema
from cryptopulse.database.session import init_engine

engine = init_engine(get_settings().database)
# `init_engine` already migrated; calling again is a no-op that reports what a
# fresh run would add, which is zero. Ask the migrator directly instead.
added = ensure_schema(engine)
print(f"    {len(added)} colonne(s) supplementaire(s) apres migration")
PYEOF
ok "vos signaux, décisions et positions sont intacts"

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────┐
  │  Mise à jour terminée.                                 │
  │                                                        │
  │  Démarrer :  ./android-start.sh                        │
  │                                                        │
  │  En cas de problème, restaurer la base :               │
  │    cp data/cryptopulse.db.backup data/cryptopulse.db   │
  └────────────────────────────────────────────────────────┘

BANNER
