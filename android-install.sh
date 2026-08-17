#!/usr/bin/env bash
# CRYPTO PULSE AI — installation Android. À lancer UNE SEULE FOIS.
#
#   ./android-install.sh
#
# Fait tout ce qui est lent : environnement Python, dépendances, icônes,
# construction de l'interface. Ensuite, le démarrage quotidien
# (./android-start.sh) ne fait plus que démarrer, en une seconde ou deux.
#
# POURQUOI TROIS SCRIPTS AU LIEU D'UN
#
# L'ancien android-start.sh faisait tout à chaque lancement : vérifier pip,
# chercher les sources plus récentes que le bundle, éventuellement relancer npm,
# puis attendre la fin d'un `doctor` réseau avant même de démarrer le serveur.
# Sur un téléphone cela faisait des dizaines de secondes d'écran vide pour un
# travail déjà fait la veille. Installer, mettre à jour et démarrer sont trois
# choses différentes, à des fréquences différentes.

set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
PY="$VENV/bin/python"

say()  { printf '\n\033[36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$1"; }

# ------------------------------------------------------------------ python ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 est absent. Dans Termux : pkg install -y python" >&2
  exit 1
fi

say "Environnement Python"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
warn "Le téléphone compile numpy ; c'est long, et cela n'arrive qu'ici."
"$VENV/bin/pip" install -q -e "."
ok "dépendances installées"

# ------------------------------------------------------------------ icônes ---
# Python pur, sans bibliothèque d'images : pas une dépendance de plus pour six PNG.
say "Icônes"
"$PY" scripts/make_icons.py >/dev/null
ok "icônes générées"

# --------------------------------------------------------------- interface ---
say "Interface"
if command -v npm >/dev/null 2>&1; then
  (cd frontend && npm install --silent && npm run build)
  ok "interface construite dans frontend/dist"
else
  warn "npm absent : l'API fonctionnera, mais SANS interface."
  warn "Dans Termux : pkg install -y nodejs-lts, puis relancez ce script."
fi

# ------------------------------------------------------------ vérification ---
say "Vérification du flux de données"
warn "Ceci est le SEUL moment où la vérification bloque. Au démarrage quotidien"
warn "elle tourne en arrière-plan et n'empêche jamais l'interface de s'afficher."
if "$PY" -m cryptopulse.cli doctor; then
  ok "flux vérifié"
else
  warn "Le flux n'est pas vérifié — voir le diagnostic ci-dessus."
  warn "L'application démarrera quand même : les scans enregistrés restent"
  warn "visibles avec leur âge, et la vérification est retentée en arrière-plan."
fi

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────┐
  │  Installation terminée.                                │
  │                                                        │
  │  Au quotidien :   ./android-start.sh                   │
  │  Après un git pull : ./android-update.sh               │
  └────────────────────────────────────────────────────────┘

BANNER
