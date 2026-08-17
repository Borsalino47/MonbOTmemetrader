#!/usr/bin/env bash
# CRYPTO PULSE AI — mise à jour. À LANCER DEPUIS TERMUX NATIF, après un git pull.
#
#   ./android-update.sh
#
# Répartit chaque étape du bon côté :
#
#   1. sauvegarde SQLite            (chemin demandé à l'application, pas au shell)
#   2. dépendances Python           → UBUNTU
#   3. build React/Vite             → TERMUX NATIF   (plante sous PRoot)
#   4. dist disponible côté Ubuntu  → aucun copiage : c'est le même dossier
#   5. migration du schéma          → UBUNTU
#   6. confirmation
#
# Ne touche jamais .env ni data/ : ils ne sont pas suivis par Git, et la
# migration est additive uniquement — elle ajoute des colonnes et refuse toute
# opération destructive plutôt que de la deviner.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/android_env.sh
source "$ROOT/scripts/android_env.sh"

cp_say "Environnement détecté : $(cp_where)"
if cp_is_distro; then
  cp_die "Lancez ce script depuis TERMUX NATIF, pas depuis Ubuntu." \
         "Le build du frontend doit tourner côté Termux (Vite plante sous PRoot)." \
         "Tapez 'exit' pour quitter Ubuntu, puis relancez."
fi

# ------------------------------------------------------------ 1. sauvegarde --
# Le chemin vient de l'application, pas du shell. La variable d'environnement
# vit souvent dans .env sans jamais atteindre le shell : la déréférencer sous
# `set -u` faisait échouer le script AVANT la sauvegarde — l'étape de sauvegarde
# devenant la raison de son absence. `cp_db_path` interroge les settings Python.
cp_say "Sauvegarde de la base"
DB="$(cp_db_path "$ROOT" || true)"

if [ -z "${DB:-}" ]; then
  cp_warn "Aucune base SQLite à sauvegarder (Postgres, mémoire, ou backend non installé)."
  cp_warn "La mise à jour continue ; rien à perdre côté fichier."
else
  case "$DB" in /*) DB_ABS="$DB" ;; *) DB_ABS="$ROOT/$DB" ;; esac
  if [ -f "$DB_ABS" ]; then
    cp "$DB_ABS" "$DB_ABS.backup"
    # Vérifiée, pas supposée : une sauvegarde qu'on croit avoir est pire que
    # pas de sauvegarde du tout, parce qu'on migre en confiance.
    [ -f "$DB_ABS.backup" ] || cp_die "La sauvegarde n'a pas été écrite : $DB_ABS.backup"
    cp_ok "$(basename "$DB_ABS").backup ($(du -h "$DB_ABS.backup" | cut -f1))"
  else
    cp_warn "Base absente ($DB_ABS) — premier lancement, rien à sauvegarder."
  fi
fi

# ------------------------------------------------- 2. Python — DANS UBUNTU ---
cp_say "Dépendances Python — DANS UBUNTU"
cp_run_in_ubuntu "$ROOT" '
  set -e
  [ -x .venv/bin/python ] || { echo "    .venv absent — lancez ./android-install.sh"; exit 1; }
  .venv/bin/pip install -q -e .
'
cp_ok "à jour"

# ------------------------------------------- 3. frontend — DANS TERMUX -------
NEEDS_BUILD=0
[ ! -f "$ROOT/frontend/dist/index.html" ] && NEEDS_BUILD=1
if [ -f "$ROOT/frontend/dist/index.html" ] && [ -n "$(find "$ROOT/frontend/src" "$ROOT/frontend/public" "$ROOT/frontend/index.html" -newer "$ROOT/frontend/dist/index.html" 2>/dev/null | head -1)" ]; then
  NEEDS_BUILD=1
fi

if [ "$NEEDS_BUILD" = "1" ]; then
  cp_say "Interface — DANS TERMUX NATIF (Vite plante sous PRoot)"
  command -v npm >/dev/null 2>&1 || cp_die "npm absent. pkg install -y nodejs-lts"
  cp_run_in_termux "$ROOT" 'cd frontend && npm install --silent && npx vite build'
  [ -f "$ROOT/frontend/dist/index.html" ] || cp_die "Le build n'a rien produit."
  cp_ok "frontend/dist reconstruit"
else
  cp_ok "interface déjà à jour, rien à reconstruire"
fi

# --------------------------------------------- 4. dist côté Ubuntu -----------
# Rien à copier dans le cas normal : le projet est monté au MÊME chemin dans
# Ubuntu, donc frontend/dist est littéralement le même dossier. Le copiage
# n'existe que si vous maintenez volontairement un second dépôt côté Ubuntu.
if [ -n "${CP_UBUNTU_PROJECT:-}" ]; then
  cp_say "Copie de frontend/dist vers $CP_UBUNTU_PROJECT"
  cp_run_in_ubuntu "$ROOT" "
    mkdir -p '$CP_UBUNTU_PROJECT/frontend'
    rm -rf '$CP_UBUNTU_PROJECT/frontend/dist'
    cp -r '$ROOT/frontend/dist' '$CP_UBUNTU_PROJECT/frontend/dist'
  "
  cp_ok "copié (dépôt Ubuntu séparé)"
else
  cp_ok "dist accessible depuis Ubuntu — même dossier, aucun copiage"
fi

# ------------------------------------------- 5. migration — DANS UBUNTU ------
cp_say "Schéma de la base — DANS UBUNTU"
cp_run_in_ubuntu "$ROOT" '
  .venv/bin/python - <<PYEOF
from cryptopulse.config.settings import get_settings
from cryptopulse.database.migrate import ensure_schema
from cryptopulse.database.session import init_engine

engine = init_engine(get_settings().database)
added = ensure_schema(engine)
print(f"    {len(added)} colonne(s) restante(s) a ajouter apres migration")
PYEOF
'
cp_ok "vos signaux, décisions et positions sont intacts"

# ----------------------------------------------------------- 6. confirmation -
cp_say "Vérification finale"
cp_run_in_ubuntu "$ROOT" '.venv/bin/python -c "import cryptopulse; print(\"    backend importable\")"'
[ -f "$ROOT/frontend/dist/index.html" ] && cp_ok "interface présente"

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────┐
  │  Mise à jour terminée. Démarrer :                      │
  │                                                        │
  │    ./android-start.sh                                  │
  │                                                        │
  │  En cas de problème, restaurer la base :               │
  │    cp data/cryptopulse.db.backup data/cryptopulse.db   │
  └────────────────────────────────────────────────────────┘

BANNER
