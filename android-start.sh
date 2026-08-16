#!/usr/bin/env bash
# CRYPTO PULSE AI — démarrage Android, une seule commande.
#
#   ./android-start.sh          installe ce qu'il manque, construit, démarre
#   ./android-start.sh demo     idem, en données SYNTHÉTIQUES (hors-ligne)
#
# Puis ouvrez http://127.0.0.1:8000 dans Chrome, menu ⋮ → « Installer
# l'application ». Une icône CRYPTO PULSE apparaît sur l'écran d'accueil.
#
# POURQUOI 127.0.0.1 ET PAS L'ADRESSE RÉSEAU DU TÉLÉPHONE
#
# L'installation d'une PWA et le service worker exigent un « contexte
# sécurisé ». La spécification traite localhost / 127.0.0.1 comme sécurisé au
# même titre que HTTPS — c'est ce qui rend l'installation possible ici sans
# certificat. Depuis un autre appareil (192.168.x.x), la page fonctionne mais
# ne peut pas être installée. L'application le dit elle-même dans ce cas.

set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-live}"
VENV=.venv
PY="$VENV/bin/python"

say() { printf '\n\033[36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }

# ------------------------------------------------------------------ python ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 est absent. Dans Termux : pkg install -y python" >&2
  exit 1
fi

if [ ! -x "$PY" ]; then
  say "Création de l'environnement Python"
  python3 -m venv "$VENV"
fi

if ! "$PY" -c "import cryptopulse" >/dev/null 2>&1; then
  say "Installation des dépendances (quelques minutes la première fois)"
  warn "Le téléphone compile numpy ; c'est normal et cela n'arrive qu'une fois."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "."
fi

# ------------------------------------------------------------------ icônes ---
# Générées en Python pur, sans bibliothèque d'images : pas de dépendance de plus
# pour six PNG.
if [ ! -f frontend/public/icons/icon-512.png ]; then
  say "Génération des icônes"
  "$PY" scripts/make_icons.py
fi

# --------------------------------------------------------------- interface ---
NEEDS_BUILD=0
[ ! -f frontend/dist/index.html ] && NEEDS_BUILD=1
# Reconstruire si une source est plus récente que le bundle.
if [ -f frontend/dist/index.html ] && [ -n "$(find frontend/src frontend/public frontend/index.html -newer frontend/dist/index.html 2>/dev/null | head -1)" ]; then
  NEEDS_BUILD=1
fi

if [ "$NEEDS_BUILD" = "1" ]; then
  if command -v npm >/dev/null 2>&1; then
    say "Construction de l'interface"
    (cd frontend && npm install --silent && npm run build)
  else
    warn "npm absent : l'API démarrera sans l'interface."
    warn "Dans Termux : pkg install -y nodejs-lts"
    warn "Sans interface construite, l'application n'est pas installable."
  fi
fi

# ------------------------------------------------------------ vérifications --
if [ "$MODE" = "demo" ]; then
  export CP_PROVIDER_MARKET_DATA=fixture
  warn "MODE DÉMO : toutes les valeurs sont générées. Aucune ne vient d'un marché."
else
  say "Vérification du flux de données"
  if ! "$PY" -m cryptopulse.cli doctor; then
    echo
    warn "Le flux n'est pas vérifié — voir le diagnostic ci-dessus."
    warn "Pour essayer l'interface malgré tout : ./android-start.sh demo"
    exit 1
  fi
fi

# ------------------------------------------------------------------ départ ---
cat <<'BANNER'

  ┌────────────────────────────────────────────────────────┐
  │  CRYPTO PULSE AI                                       │
  │                                                        │
  │  Ouvrez  http://127.0.0.1:8000  dans Chrome            │
  │                                                        │
  │  Pour l'installer sur l'écran d'accueil :              │
  │    menu ⋮  →  « Installer l'application »              │
  │                                                        │
  │  Laissez cette fenêtre Termux ouverte.                 │
  │  Arrêter : Ctrl+C                                      │
  └────────────────────────────────────────────────────────┘

BANNER

# 127.0.0.1 et non 0.0.0.0 : le contexte sécurisé exigé par l'installation vaut
# pour la boucle locale, et cela évite d'exposer le scanner au réseau Wi-Fi.
exec "$PY" -m cryptopulse.cli serve --host 127.0.0.1 --port 8000
