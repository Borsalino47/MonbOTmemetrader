#!/usr/bin/env bash
# CRYPTO PULSE AI — installation Android. À LANCER DEPUIS TERMUX NATIF, une fois.
#
#   ./android-install.sh
#
# ARCHITECTURE HYBRIDE
#
#     TERMUX NATIF     Node.js / npm / Vite   → construit frontend/dist
#          ↓ bind
#     UBUNTU (proot)   Python / FastAPI / SQLite → fait tourner le serveur
#
# Les deux moitiés sont obligatoires et aucune ne peut faire le travail de
# l'autre sur cet appareil :
#
#   * `npx vite build` sous Ubuntu/PRoot plante en BUS ERROR (esbuild émet des
#     instructions que la traduction d'appels système de PRoot ne supporte pas).
#     Le build DOIT tourner dans Termux natif.
#   * numpy et pydantic-core ne se compilent pas proprement dans Termux natif.
#     Le Python qui marche est celui d'Ubuntu.
#
# Ce script sait de quel côté va chaque commande. Vous n'avez jamais à taper
# `proot-distro login` vous-même.

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

# ------------------------------------------------------- Ubuntu : la base ----
if ! cp_have_proot_distro; then
  cp_say "Installation de proot-distro"
  pkg install -y proot-distro
fi

if ! proot-distro list --installed 2>/dev/null | grep -qi "^${CP_DISTRO}\b" \
   && [ ! -d "${PREFIX:-/data/data/com.termux/files/usr}/var/lib/proot-distro/installed-rootfs/${CP_DISTRO}" ]; then
  cp_say "Installation d'Ubuntu (long, une seule fois)"
  proot-distro install "$CP_DISTRO"
fi
cp_ok "Ubuntu disponible"

# ------------------------------------------------- Ubuntu : Python + deps ----
cp_say "Python et dépendances — DANS UBUNTU"
cp_warn "Le téléphone compile numpy ; c'est long, et cela n'arrive qu'ici."
cp_run_in_ubuntu "$ROOT" '
  set -e
  # The marker the other scripts detect. Written rather than guessed: sniffing
  # /etc/os-release cannot tell Ubuntu-under-PRoot-on-a-phone from any other
  # Ubuntu, and a desktop misidentified as the container refuses to build.
  touch /etc/cryptopulse-inside-distro 2>/dev/null || true
  command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-dev build-essential; }
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e .
  .venv/bin/python -c "import cryptopulse, numpy, fastapi; print(\"    backend OK\")"
'
cp_ok "backend installé dans Ubuntu"

# ------------------------------------------------------ Ubuntu : icônes ------
# Python pur, pas de bibliothèque d'images : autant le faire du côté qui a Python.
cp_say "Icônes — DANS UBUNTU"
cp_run_in_ubuntu "$ROOT" '.venv/bin/python scripts/make_icons.py >/dev/null'
cp_ok "icônes générées"

# ------------------------------------------------ Termux : build frontend ----
cp_say "Interface — DANS TERMUX NATIF (Vite plante sous PRoot)"
if ! command -v npm >/dev/null 2>&1; then
  cp_warn "npm absent. Installation : pkg install -y nodejs-lts"
  pkg install -y nodejs-lts
fi
cp_run_in_termux "$ROOT" 'cd frontend && npm install --silent && npx vite build'
[ -f "$ROOT/frontend/dist/index.html" ] \
  || cp_die "Le build n'a pas produit frontend/dist/index.html."
cp_ok "frontend/dist construit"

# --------------------------------------------------- Ubuntu : le flux --------
cp_say "Vérification du flux — DANS UBUNTU"
cp_warn "C'est le SEUL moment où la vérification bloque. Au démarrage quotidien"
cp_warn "elle tourne en arrière-plan et n'empêche jamais l'affichage."
if cp_run_in_ubuntu "$ROOT" '.venv/bin/python -m cryptopulse.cli doctor'; then
  cp_ok "flux vérifié"
else
  cp_warn "Flux non vérifié — voir le diagnostic ci-dessus."
  cp_warn "L'application démarrera quand même : les scans enregistrés restent"
  cp_warn "visibles avec leur âge, et la vérification est retentée en arrière-plan."
fi

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────┐
  │  Installation terminée.                                │
  │                                                        │
  │  Depuis TERMUX, toujours :                             │
  │    tous les jours       ./android-start.sh             │
  │    après un git pull    ./android-update.sh            │
  │                                                        │
  │  Vous n'avez jamais à entrer dans Ubuntu vous-même.    │
  └────────────────────────────────────────────────────────┘

BANNER
