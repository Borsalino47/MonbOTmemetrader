#!/usr/bin/env bash
# CRYPTO PULSE AI — démarrage quotidien. Ne fait QUE démarrer.
#
#   ./android-start.sh          démarre
#   ./android-start.sh demo     démarre en données SYNTHÉTIQUES (hors-ligne)
#
# Puis ouvrez http://127.0.0.1:8000 dans Chrome.
#
# CE QUE CE SCRIPT NE FAIT PLUS, ET POURQUOI
#
# Plus de pip, plus de npm, plus de génération d'icônes, plus de `doctor`
# bloquant. Chacune de ces étapes coûtait des secondes à chaque lancement pour
# refaire un travail déjà fait — et le `doctor` en coûtait le plus, car il
# attendait le réseau avant même de démarrer le serveur. L'interface attendait
# une vérification dont l'affichage du dernier scan enregistré n'a pas besoin.
#
# Installation et mise à jour vivent maintenant dans leurs propres scripts :
#   ./android-install.sh   une fois
#   ./android-update.sh    après un git pull
#
# La vérification du flux tourne désormais DANS l'application, en arrière-plan.
# L'écran affiche 🟡 pendant, puis 🟢 ou 🔴. Tant qu'elle n'a pas réussi, les
# scans enregistrés restent visibles avec leur âge, et aucune nouvelle décision
# n'est présentée comme vérifiée en direct.
#
# POURQUOI 127.0.0.1 ET PAS L'ADRESSE RÉSEAU DU TÉLÉPHONE
#
# L'installation d'une PWA exige un « contexte sécurisé ». La spécification
# traite 127.0.0.1 comme sécurisé au même titre que HTTPS — c'est ce qui rend
# l'installation possible sans certificat. Cela évite aussi d'exposer le
# scanner au Wi-Fi.

set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-live}"
PY=.venv/bin/python

if [ ! -x "$PY" ]; then
  echo "Environnement absent. Lancez d'abord : ./android-install.sh" >&2
  exit 1
fi

if [ ! -f frontend/dist/index.html ]; then
  # Diagnostiqué ici plutôt que reconstruit : reconstruire au démarrage est
  # exactement ce que cette phase supprime, et une minute de npm surprise est
  # pire qu'un message clair.
  printf '\033[33m    Interface non construite — l API démarrera sans elle.\033[0m\n'
  printf '\033[33m    Pour la construire : ./android-update.sh\033[0m\n'
fi

if [ "$MODE" = "demo" ]; then
  export CP_PROVIDER_MARKET_DATA=fixture
  printf '\033[33m    MODE DÉMO : toutes les valeurs sont générées.\033[0m\n'
fi

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────┐
  │  CRYPTO PULSE AI                                       │
  │                                                        │
  │  Ouvrez  http://127.0.0.1:8000  dans Chrome            │
  │                                                        │
  │  L'interface s'affiche immédiatement avec le dernier   │
  │  scan enregistré. La vérification du flux se fait      │
  │  ensuite, en arrière-plan :                            │
  │                                                        │
  │    🟡 vérification en cours                            │
  │    🟢 flux vérifié                                     │
  │    🔴 flux non vérifié                                 │
  │                                                        │
  │  Arrêter : Ctrl+C                                      │
  └────────────────────────────────────────────────────────┘

BANNER

exec "$PY" -m cryptopulse.cli serve --host 127.0.0.1 --port 8000
