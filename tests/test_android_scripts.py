"""The hybrid Android architecture, enforced rather than documented.

    TERMUX NATIF          Node.js / npm / Vite  ->  builds frontend/dist
         |  bind mount
    UBUNTU (proot-distro) Python / FastAPI / SQLite  ->  runs the server

Neither half is optional and neither can do the other's job on the target
device. `npx vite build` inside Ubuntu under PRoot dies with a BUS ERROR —
esbuild issues instructions PRoot's syscall translation does not survive — and
numpy and pydantic-core do not build cleanly in Termux natively.

So putting a command on the wrong side is not a slow path, it is a crash, and a
future edit that quietly moves one is exactly the kind of change that looks
harmless in review. These tests read the scripts and assert the split.

WHAT THEY CAN AND CANNOT PROVE

They run the real scripts with a stand-in `proot-distro` on PATH that records
its argv, so the delegation — which command text is handed to which environment
— is genuinely exercised rather than inspected. What they cannot prove is that
proot-distro on a real phone behaves as documented: this is not an Android
device, and nothing here has spoken to one.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("android-install.sh", "android-update.sh", "android-start.sh")

# Anything that must never be handed to Ubuntu.
FRONTEND_BUILD_TOKENS = ("vite build", "npm run build", "npm install", "esbuild")
# Anything that must never be run natively in Termux.
HEAVY_PYTHON_TOKENS = ("pip install", "python3 -m venv", "make_icons.py")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _blocks(text: str, fn: str) -> list[str]:
    """Every argument block passed to `fn`, comments stripped.

    Deliberately a real scan rather than a regex over the whole file: the check
    is worthless if it cannot tell a command inside a `cp_run_in_ubuntu` heredoc
    from the same word in a comment two lines above. Handles both the single and
    double quoted forms the scripts use, across multiple lines.
    """
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if fn not in line:
            i += 1
            continue
        # The command argument starts at the first quote after the root path.
        rest = line.split(fn, 1)[1]
        quote = None
        for ch in rest:
            if ch in ("'", '"'):
                if quote is None:
                    quote = ch
                elif ch == quote:
                    quote = None
        # Unbalanced quote on this line means the block continues.
        collected = [rest]
        while quote is not None and i + 1 < len(lines):
            i += 1
            collected.append(lines[i])
            for ch in lines[i]:
                if ch == quote:
                    quote = None
                    break
        out.append("\n".join(collected))
        i += 1
    return out


def _ubuntu_blocks(text: str) -> list[str]:
    return _blocks(text, "cp_run_in_ubuntu")


def _termux_blocks(text: str) -> list[str]:
    return _blocks(text, "cp_run_in_termux")


# --------------------------------------------------------------------------- #
# Static structure
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_parses(name):
    """A syntax error in a launcher is only discovered on the phone."""
    result = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_is_executable(name):
    assert (ROOT / name).stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_sources_the_shared_environment_helpers(name):
    """One definition of "which side does this run on". Two would drift."""
    assert "scripts/android_env.sh" in _read(name)


def test_the_shared_helpers_parse():
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / "android_env.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# The split — the property the device depends on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_frontend_build_is_ever_handed_to_ubuntu(name):
    """`npx vite build` under PRoot is a BUS ERROR, not a slow build.

    Checked by reading every `cp_run_in_ubuntu` block in the script and
    asserting no build token appears inside it.
    """
    text = _read(name)
    for block in _ubuntu_blocks(text):
        for token in FRONTEND_BUILD_TOKENS:
            assert token not in block, (
                f"{name}: '{token}' is inside a cp_run_in_ubuntu block.\n"
                f"Vite/esbuild crash under PRoot — this must run in Termux.\n{block}"
            )


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_heavy_python_install_runs_natively_in_termux(name):
    """numpy and pydantic-core do not build cleanly in Termux natively."""
    text = _read(name)
    for block in _termux_blocks(text):
        for token in HEAVY_PYTHON_TOKENS:
            assert token not in block, (
                f"{name}: '{token}' is inside a cp_run_in_termux block.\n"
                f"The working Python stack is the one inside Ubuntu.\n{block}"
            )


def test_the_frontend_build_is_delegated_to_termux_explicitly():
    """Not merely absent from the Ubuntu side — present on the Termux side, so
    the split is asserted rather than inferred from an absence."""
    for name in ("android-install.sh", "android-update.sh"):
        blocks = "\n".join(_termux_blocks(_read(name)))
        assert "vite build" in blocks, f"{name} never builds the frontend in Termux"


def test_python_work_is_delegated_to_ubuntu_explicitly():
    for name in ("android-install.sh", "android-update.sh"):
        blocks = "\n".join(_ubuntu_blocks(_read(name)))
        assert "pip install" in blocks, f"{name} never installs Python deps in Ubuntu"


def test_the_helpers_refuse_a_frontend_build_from_inside_ubuntu():
    """`cp_run_in_termux` called from inside Ubuntu must fail loudly. Failing
    quietly there is precisely the bus error, several minutes later."""
    helpers = _read("scripts/android_env.sh")
    body = helpers.split("cp_run_in_termux()", 1)[1].split("\n}", 1)[0]
    assert "cp_is_distro" in body
    assert "cp_die" in body
    assert "BUS ERROR" in body


# --------------------------------------------------------------------------- #
# The daily launcher stays fast
# --------------------------------------------------------------------------- #


def test_the_daily_launcher_installs_and_builds_nothing():
    """The whole point of splitting the scripts. A pip or npm call creeping back
    in here is the regression that made the app slow to start."""
    text = _read("android-start.sh")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for token in ("pip install", "npm install", "npm run build", "vite build", "make_icons"):
        assert token not in body, f"android-start.sh must not run '{token}'"


def test_the_daily_launcher_does_not_block_on_the_feed_check():
    """`doctor` here is what put a network round trip in front of a blank
    screen. It belongs to install, and to the background task in the service."""
    body = "\n".join(
        line for line in _read("android-start.sh").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "cli doctor" not in body


def test_the_daily_launcher_serves_on_loopback():
    """127.0.0.1 is what makes the PWA installable without a certificate, and it
    keeps the scanner off the Wi-Fi."""
    assert "--host 127.0.0.1" in _read("android-start.sh")


# --------------------------------------------------------------------------- #
# Delegation, actually executed
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fake_proot(tmp_path):
    """A real executable named `proot-distro` that records its argv.

    The same technique as the Termux notification tests: the subprocess path is
    genuinely exercised, so what is asserted is the command that would really
    have been sent rather than a reading of the source.
    """
    log = tmp_path / "argv.log"
    binary = tmp_path / "proot-distro"
    binary.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        "exit 0\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary.parent, log


def _run_helper(fake_proot, snippet: str) -> tuple[subprocess.CompletedProcess, str]:
    """Source the helpers and run `snippet`, with the fake proot-distro on PATH."""
    fake_dir, log = fake_proot
    script = (
        f"set -euo pipefail\n"
        f"source '{ROOT / 'scripts' / 'android_env.sh'}'\n"
        f"{snippet}\n"
    )
    # Forced: this machine is neither Termux nor the container, so without the
    # override `cp_run_in_ubuntu` would correctly run locally and never exercise
    # the delegation this test exists to check.
    env = dict(os.environ, PATH=f"{fake_dir}:{os.environ['PATH']}", CP_FORCE_ENV="termux")
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, cwd=str(ROOT)
    )
    return result, log.read_text() if log.exists() else ""


def test_ubuntu_delegation_binds_the_project_at_the_same_path(fake_proot):
    """The identical path is what removes the copying. A relative path written
    on one side resolves to the same file on the other, so `.env`, `data/` and
    `frontend/dist` need no translation at all."""
    result, argv = _run_helper(fake_proot, f"cp_run_in_ubuntu '{ROOT}' 'echo hi'")

    assert result.returncode == 0, result.stderr
    assert f"--bind {ROOT}:{ROOT}" in argv
    assert "login ubuntu" in argv
    assert "echo hi" in argv


def test_ubuntu_delegation_changes_into_the_project_first(fake_proot):
    _, argv = _run_helper(fake_proot, f"cp_run_in_ubuntu '{ROOT}' 'pwd'")
    assert f"cd '{ROOT}'" in argv


def test_a_missing_proot_distro_is_named_rather_than_a_command_not_found():
    """"proot-distro: command not found" tells the user nothing about which of
    two installs is missing — the Termux package, or the distribution itself.

    No fake binary here: proot-distro is genuinely absent from this machine,
    which is the condition being tested."""
    script = (
        "set -euo pipefail\n"
        f"source '{ROOT / 'scripts' / 'android_env.sh'}'\n"
        f"cp_run_in_ubuntu '{ROOT}' 'echo hi'\n"
    )
    env = dict(os.environ, CP_FORCE_ENV="termux")
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    assert result.returncode != 0
    assert "proot-distro" in result.stderr
    assert "pkg install" in result.stderr


def test_termux_delegation_runs_locally(fake_proot):
    """No container for the Termux side — it is already the native environment."""
    result, argv = _run_helper(fake_proot, f"cp_run_in_termux '{ROOT}' 'echo built'")

    assert result.returncode == 0, result.stderr
    assert "built" in result.stdout
    assert argv == "", "the frontend build must never reach proot-distro"


# --------------------------------------------------------------------------- #
# The CP_DB_URL bug
# --------------------------------------------------------------------------- #


def test_db_path_resolves_without_cp_db_url_in_the_environment():
    """The reported bug. `CP_DB_URL` lives in `.env` and never reaches the
    shell, so `${CP_DB_URL##*sqlite:///}` under `set -u` aborted the script
    before it could back anything up — the backup step becoming the reason
    there was no backup."""
    env = {k: v for k, v in os.environ.items() if k != "CP_DB_URL"}
    result = subprocess.run(
        [".venv/bin/python", "-m", "cryptopulse.cli", "db-path"],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(".db")


def test_db_path_follows_the_configured_url():
    env = dict(os.environ, CP_DB_URL="sqlite:///data/elsewhere.db")
    result = subprocess.run(
        [".venv/bin/python", "-m", "cryptopulse.cli", "db-path"],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )
    assert result.stdout.strip() == "data/elsewhere.db"


def test_db_path_prints_nothing_when_there_is_no_file_to_back_up():
    """Printing something file-shaped for Postgres would invite `cp` to create
    a garbage path, and the script would then report a backup it did not make."""
    for url in ("postgresql://user@host/db", "sqlite:///:memory:"):
        result = subprocess.run(
            [".venv/bin/python", "-m", "cryptopulse.cli", "db-path"],
            capture_output=True, text=True, cwd=str(ROOT),
            env=dict(os.environ, CP_DB_URL=url),
        )
        assert result.returncode == 1, url
        assert result.stdout.strip() == "", url


def test_update_never_dereferences_cp_db_url_directly():
    """The shape of the original bug, banned by name so it cannot come back in
    a later edit that looks like a simplification."""
    text = "\n".join(
        ln for ln in _read("android-update.sh").splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "CP_DB_URL##" not in text
    assert re.search(r'\$\{CP_DB_URL[^:}]*\}', text) is None, (
        "android-update.sh must not read CP_DB_URL without a default — "
        "use cp_db_path, which asks the application"
    )


def test_update_verifies_the_backup_before_migrating():
    """A backup one believes in is worse than none, because the migration then
    proceeds in confidence."""
    text = _read("android-update.sh")
    backup_at = text.index(".backup")
    migrate_at = text.index("ensure_schema")
    assert backup_at < migrate_at, "the backup must be taken before the migration"
    assert 'cp_die "La sauvegarde' in text, "an unwritten backup must abort the update"


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #


def test_the_split_is_documented_where_someone_would_look():
    for doc in ("CLAUDE.md", "INSTALL.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "PRoot" in text or "proot" in text, doc
        assert "Termux" in text, doc
