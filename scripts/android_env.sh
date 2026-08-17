#!/usr/bin/env bash
# Shared plumbing for the three Android scripts. Sourced, never executed.
#
# THE ARCHITECTURE THIS EXISTS FOR
#
#     TERMUX NATIF          Node.js / npm / Vite  →  builds frontend/dist
#          ↓ bind mount
#     UBUNTU (proot-distro) Python / FastAPI / SQLite  →  runs the server
#
# Neither half is optional and neither can do the other's job on this device:
#
#   * `npx vite build` inside Ubuntu under PRoot dies with a BUS ERROR. esbuild
#     issues instructions PRoot's syscall translation does not survive. This is
#     not a configuration problem to be fixed by flags — the build must run in
#     Termux natively.
#
#   * numpy and pydantic-core do not build cleanly in Termux natively. The
#     working Python stack is the one inside Ubuntu.
#
# So every command belongs to exactly one side, and getting it wrong is not a
# slow path — it is a crash. `cp_run_in_ubuntu` and `cp_run_in_termux` make the
# side explicit at every call so a mistake is visible in the diff rather than at
# 7am on a phone.
#
# ONE REPOSITORY, NOT TWO
#
# The project lives once, in Termux home, and is bind-mounted into Ubuntu at the
# SAME path. `frontend/dist` therefore needs no copying and `data/cryptopulse.db`
# is literally the same file from both sides — which removes the entire class of
# bug where two copies drift apart and the user cannot tell which one the server
# actually read.
#
# If you genuinely keep a separate checkout inside Ubuntu, set
# `CP_UBUNTU_PROJECT` to its path and the scripts sync `frontend/dist` into it.
# Supported, but not the recommended shape.
#
# WHY A MARKER FILE INSTEAD OF SNIFFING /etc/os-release
#
# The first version guessed: "no Termux prefix and /etc/os-release says Ubuntu,
# therefore I am inside PRoot". That is true on the phone and equally true on
# any ordinary Ubuntu machine, so a desktop or CI run was misidentified as being
# inside the container and refused to build the frontend. Detection now rests on
# a marker `android-install.sh` writes inside the distribution — a fact we
# create rather than a coincidence we interpret.

CP_DISTRO="${CP_DISTRO:-ubuntu}"
# Written inside the distribution by android-install.sh. Its presence is the
# only thing that means "I am running inside the Crypto Pulse container".
CP_DISTRO_MARKER="${CP_DISTRO_MARKER:-/etc/cryptopulse-inside-distro}"

# ---------------------------------------------------------------- detection --
#
# `CP_FORCE_ENV` overrides all of it: `termux`, `distro`, or `local`. Exists for
# the tests, and for the case where someone's setup does not match the shapes
# below — an override beats a wrong guess with no way out.

cp_is_termux() {
  case "${CP_FORCE_ENV:-}" in termux) return 0 ;; distro|local) return 1 ;; esac
  [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux/files/usr ]
}

cp_is_distro() {
  case "${CP_FORCE_ENV:-}" in distro) return 0 ;; termux|local) return 1 ;; esac
  [ -f "$CP_DISTRO_MARKER" ]
}

cp_have_proot_distro() {
  command -v proot-distro >/dev/null 2>&1
}

# ------------------------------------------------------------- delegation ---

# Run a command where Python lives.
#
# Three cases, and only one of them involves a container:
#   * already inside the distribution  → run here
#   * on Termux                        → hand to proot-distro
#   * anywhere else (desktop, CI)      → run here; there is no split to make
cp_run_in_ubuntu() {
  local root="$1"; shift
  if cp_is_distro || ! cp_is_termux; then
    ( cd "$root" && bash -lc "$*" )
    return $?
  fi
  if ! cp_have_proot_distro; then
    cp_die "proot-distro est absent. Dans Termux : pkg install -y proot-distro" \
           "puis : proot-distro install $CP_DISTRO"
  fi
  # The identical path on both sides is what removes the copying: a relative
  # path written in one environment resolves to the same file in the other.
  proot-distro login "$CP_DISTRO" --bind "$root:$root" -- \
    bash -lc "cd '$root' && $*"
}

# Run a command where Node lives. A hard error from inside the distribution —
# a frontend build reaching that side is the BUS ERROR, and failing loudly here
# beats failing obscurely several minutes later.
cp_run_in_termux() {
  local root="$1"; shift
  if cp_is_distro; then
    cp_die "Cette étape doit tourner dans Termux natif, pas dans la distribution." \
           "Vite/esbuild plante (BUS ERROR) sous PRoot." \
           "Tapez 'exit' pour quitter, puis relancez depuis Termux."
  fi
  ( cd "$root" && bash -lc "$*" )
}

# ------------------------------------------------------------------ output --

cp_say()  { printf '\n\033[36m==>\033[0m %s\n' "$1"; }
cp_ok()   { printf '\033[32m    %s\033[0m\n' "$1"; }
cp_warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
cp_die()  { printf '\033[31m    %s\033[0m\n' "$@" >&2; exit 1; }

cp_where() {
  if cp_is_distro; then echo "$CP_DISTRO (proot)"
  elif cp_is_termux; then echo "Termux natif"
  else echo "$(uname -s) — pas Android, aucune séparation à faire"; fi
}

# -------------------------------------------------------------------- paths --

# The SQLite file, asked of the application rather than parsed out of the shell.
#
# `CP_DB_URL` commonly lives in `.env` and never reaches the shell environment,
# so `${CP_DB_URL##*sqlite:///}` under `set -u` aborted before the backup could
# be taken — the backup step becoming the reason there was no backup. `db-path`
# reads the same settings the server reads.
#
# Prints nothing and returns non-zero when there is no file (Postgres,
# in-memory), so callers skip the backup rather than inventing a path for `cp`.
cp_db_path() {
  local root="$1"
  cp_run_in_ubuntu "$root" \
    '.venv/bin/python -m cryptopulse.cli db-path 2>/dev/null' 2>/dev/null | tail -1
}
