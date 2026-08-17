#!/usr/bin/env bash
# CRYPTO PULSE AI — démarrage quotidien. DEPUIS TERMUX NATIF. Ne fait QUE démarrer.
#
#   ./android-start.sh          démarre
#   ./android-start.sh demo     démarre en données SYNTHÉTIQUES (hors-ligne)
#
# Puis ouvrez http://127.0.0.1:8000 dans Chrome.
#
# CE QU'IL NE FAIT PAS, ET POURQUOI
#
# Pas de pip, pas de npm, pas de génération d'icônes, pas de `doctor` bloquant.
# Chacune de ces étapes coûtait des secondes à chaque lancement pour refaire un
# travail déjà fait — et le `doctor` le plus, car il attendait le réseau avant
# même de démarrer le serveur. L'interface attendait une vérification dont
# l'affichage du dernier scan enregistré n'a pas besoin.
#
#   installation    ./android-install.sh   une fois
#   mise à jour     ./android-update.sh    après un git pull
#   démarrage       ce script              tous les jours
#
# OÙ TOURNE LE SERVEUR
#
# Dans Ubuntu (proot-distro), parce que c'est là qu'est le Python qui marche.
# Ce script s'en charge : vous ne tapez jamais `proot-distro login` vous-même.
# Le projet est monté au MÊME chemin des deux côtés, donc .env, data/ et
# frontend/dist sont les mêmes fichiers, sans copie ni traduction.
#
# POURQUOI 127.0.0.1 ET PAS L'ADRESSE WI-FI
#
# L'installation d'une PWA exige un « contexte sécurisé ». La spécification
# traite 127.0.0.1 comme sécurisé au même titre que HTTPS — c'est ce qui rend
# l'installation possible sans certificat. PRoot partage la pile réseau de
# l'appareil, donc un serveur lancé sur 127.0.0.1 dans Ubuntu est joignable
# depuis Chrome Android sans configuration.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/android_env.sh
source "$ROOT/scripts/android_env.sh"

MODE="${1:-live}"
PORT="${CP_PORT:-8000}"

if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  # Diagnostiqué, pas reconstruit : reconstruire au démarrage est exactement ce
  # que cette phase supprime, et une minute de npm surprise est pire qu'un
  # message clair.
  cp_warn "Interface non construite — l'API démarrera sans elle."
  cp_warn "Pour la construire : ./android-update.sh   (depuis Termux)"
fi

ENVVARS=""
if [ "$MODE" = "demo" ]; then
  ENVVARS="CP_PROVIDER_MARKET_DATA=fixture"
  cp_warn "MODE DÉMO : toutes les valeurs sont générées."
fi

cat <<BANNER

  ┌────────────────────────────────────────────────────────┐
  │  CRYPTO PULSE AI                                       │
  │                                                        │
  │  Ouvrez  http://127.0.0.1:$PORT  dans Chrome            │
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

# `exec` du côté Termux pour que Ctrl+C atteigne bien le serveur : sans lui, le
# signal s'arrête sur le shell intermédiaire et le serveur survit à sa fenêtre.
exec_ubuntu() {
  if cp_is_distro || ! cp_is_termux; then
    exec env $ENVVARS .venv/bin/python -m cryptopulse.cli serve --host 127.0.0.1 --port "$PORT"
  fi
  cp_have_proot_distro || cp_die \
    "proot-distro est absent. Lancez d'abord ./android-install.sh"
  exec proot-distro login "$CP_DISTRO" --bind "$ROOT:$ROOT" -- \
    bash -lc "cd '$ROOT' && exec env $ENVVARS .venv/bin/python -m cryptopulse.cli serve --host 127.0.0.1 --port $PORT"
}

cd "$ROOT"
exec_ubuntu
