#!/usr/bin/env bash
# CRYPTO PULSE AI — démarrage en une commande.
#
#   ./start.sh              scan avec le fournisseur configuré
#   ./start.sh kraken       scan via Kraken
#   ./start.sh binance      scan via Binance
#   ./start.sh demo         scan hors-ligne (données SYNTHÉTIQUES)
#   ./start.sh radar        RADAR AUTONOME : scanne, alerte, notifie, en boucle
#   ./start.sh universe     ce que le filtre Robinhood donne sur cette place
#   ./start.sh serve        API + dashboard sur http://localhost:8000
#
# Installe ce qu'il manque, vérifie le feed, puis lance. Rien d'autre à faire.

set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv
PY="$VENV/bin/python"
MODE="${1:-scan}"

# ---------------------------------------------------------------- install ---
if [ ! -x "$PY" ]; then
  echo "==> Création de l'environnement Python"
  python3 -m venv "$VENV"
fi
# On teste une dépendance, PAS `import cryptopulse` : le paquet est dans le
# répertoire courant, donc son import réussit même dans un venv vide — et
# l'installation était alors sautée, pour échouer une ligne plus loin sur
# ModuleNotFoundError. C'est le tout premier obstacle d'un clone neuf.
if ! "$PY" -c "import pydantic, fastapi, numpy, sqlalchemy, structlog, httpx" >/dev/null 2>&1; then
  echo "==> Installation des dépendances (une seule fois)"
  "$VENV/bin/pip" install -q --upgrade pip
  # Le driver PostgreSQL n'est installé que s'il sert : sinon le processus
  # démarre et meurt à la première écriture, ce qui est un mode d'échec inutile.
  if grep -qs "^CP_DB_URL=postgres" .env; then
    "$VENV/bin/pip" install -q -e ".[postgres]"
  else
    "$VENV/bin/pip" install -q -e "."
  fi
fi

# La configuration documentée doit être celle qui tourne. Sans ce fichier, les
# valeurs par défaut du code s'appliquent — et elles ne sont pas celles du
# produit (univers Robinhood, canaux d'alerte, couche ×10 notée).
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "==> .env créé depuis .env.example (édite-le pour tes canaux d'alerte)"
fi

# ------------------------------------------------------------ mode / feed ---
case "$MODE" in
  demo)     PROVIDER=fixture ;;
  kraken)   PROVIDER=kraken ;;
  binance)  PROVIDER=binance ;;
  serve)    PROVIDER="" ;;
  scan)     PROVIDER="" ;;
  radar)    PROVIDER="" ;;
  universe) PROVIDER="" ;;
  *)        echo "Mode inconnu: $MODE"
            echo "Utilisation: ./start.sh [scan|radar|universe|kraken|binance|demo|serve]"; exit 2 ;;
esac

FLAG=()
[ -n "$PROVIDER" ] && FLAG=(--provider "$PROVIDER")

# --------------------------------------------------------------- dashboard --
if [ "$MODE" = "serve" ]; then
  if [ ! -d frontend/dist ] && command -v npm >/dev/null 2>&1; then
    echo "==> Construction du dashboard (une seule fois)"
    (cd frontend && npm install --silent && npm run build)
  elif [ ! -d frontend/dist ]; then
    echo "==> npm absent : l'API démarrera sans le dashboard (les endpoints restent disponibles)"
  fi
  echo "==> http://localhost:8000"
  exec "$PY" -m cryptopulse.cli serve
fi

# ---------------------------------------------------------------- univers ---
# Ne vérifie pas le feed : la commande diagnostique elle-même ce qu'elle trouve.
if [ "$MODE" = "universe" ]; then
  exec "$PY" -m cryptopulse.cli universe
fi

# ------------------------------------------------------------------ vérif ---
# Le feed est vérifié avant de scanner : mieux vaut un diagnostic clair qu'un
# tableau de scores construit sur une source muette.
if [ "$MODE" != "demo" ]; then
  echo "==> Vérification du flux de données"
  if ! "$PY" -m cryptopulse.cli doctor "${FLAG[@]}"; then
    echo
    echo "Le flux n'est pas vérifié — voir le diagnostic ci-dessus."
    echo "Pour essayer l'outil malgré tout, en données SYNTHÉTIQUES : ./start.sh demo"
    exit 1
  fi
fi

echo
if [ "$MODE" = "radar" ]; then
  # Le radar tourne sans surveillance : il affiche d'abord où partiront les
  # alertes, pour qu'un canal mal configuré se voie tout de suite et pas à 3h14.
  exec "$PY" -m cryptopulse.cli radar "${FLAG[@]}"
fi

exec "$PY" -m cryptopulse.cli scan --limit 30 "${FLAG[@]}"
