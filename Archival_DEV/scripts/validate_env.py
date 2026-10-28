#!/usr/bin/env python3
"""
validate_env.py - Environment & Runtime Prerequisite Validator

Validates:
1. Python version (>= 3.11) and virtual environment status.
2. SQLite3 installation and WAL journal mode support.
3. Node.js binary (>= v18) and Playwright stealth npm dependencies.
4. System archive extraction binaries (7-Zip '7z' or 'unrar').
5. Filesystem write permissions for 'previews/', 'scrapes/', and 'metadata/'.

Returns exit code 0 if healthy, or 1 with actionable remediation commands.
"""

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

# Terminal color codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def get_project_root() -> Path:
    """Finds project root by locating package.json or pyproject.toml."""
    candidates = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent.parent,
    ]
    for c in candidates:
        if (c / "package.json").exists() or (c / "pyproject.toml").exists() or (c / "library.db").exists():
            return c
    return Path.cwd()


ROOT_DIR = get_project_root()


def get_node_bin() -> Optional[str]:
    node_bin = shutil.which("node") or shutil.which("nodejs")
    if not node_bin:
        for fallback in ["/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"]:
            if Path(fallback).exists():
                return fallback
    return node_bin


def check_python() -> Tuple[bool, str, str]:
    version = sys.version_info
    ver_str = f"{version.major}.{version.minor}.{version.micro}"
    in_venv = sys.prefix != sys.base_prefix or "VIRTUAL_ENV" in os.environ

    if version < (3, 11):
        return (
            False,
            f"Python {ver_str} (Unsupported)",
            "Python 3.11 or higher is required. Install via `uv python install 3.12` or `brew install python@3.12`."
        )

    venv_status = "active virtual environment" if in_venv else "system environment (virtual env recommended)"
    return True, f"Python {ver_str} ({venv_status})", ""


def check_sqlite() -> Tuple[bool, str, str]:
    ver = sqlite3.sqlite_version
    try:
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            mode = cur.fetchone()[0]
            conn.close()
            if mode.upper() != "WAL":
                return False, f"SQLite {ver} (WAL unsupported)", "SQLite database does not support WAL journal mode."
        return True, f"SQLite {ver} (WAL verified)", ""
    except Exception as e:
        return False, "SQLite Failure", f"Failed testing SQLite WAL mode: {e}"


def check_nodejs() -> Tuple[bool, str, str]:
    node_bin = get_node_bin()
    if not node_bin:
        return (
            False,
            "Node.js missing",
            "Node.js v18+ is required for the Playwright scraper. Install via `brew install node` or `apt-get install nodejs`."
        )

    try:
        res = subprocess.run([node_bin, "-v"], capture_output=True, text=True, check=True)
        v_str = res.stdout.strip()
        major = int(v_str.lstrip("v").split(".")[0])
        if major < 18:
            return (
                False,
                f"Node.js {v_str} (Outdated)",
                f"Node.js v18+ required (detected {v_str}). Upgrade via `brew upgrade node` or `nvm install 20`."
            )
        return True, f"Node.js {v_str} ({node_bin})", ""
    except Exception as e:
        return False, "Node.js Error", f"Error checking Node.js: {e}"


def check_node_packages() -> Tuple[bool, str, str]:
    node_bin = get_node_bin()
    if not node_bin:
        return False, "Node.js not found", "Install Node.js first."

    test_script = (
        "try { "
        "  require('playwright'); "
        "  require('playwright-extra'); "
        "  require('puppeteer-extra-plugin-stealth'); "
        "  console.log('OK'); "
        "} catch (e) { "
        "  console.error(e.message); "
        "  process.exit(1); "
        "}"
    )

    try:
        res = subprocess.run(
            [node_bin, "-e", test_script],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            return True, f"Playwright & stealth plugins verified in {ROOT_DIR.name}/", ""
        else:
            err_msg = res.stderr.strip() or res.stdout.strip()
            return (
                False,
                f"Node packages missing in {ROOT_DIR.name}/ ({err_msg})",
                f"Run `npm install playwright playwright-extra puppeteer-extra-plugin-stealth && npx playwright install chromium` inside {ROOT_DIR}"
            )
    except Exception as e:
        return (
            False,
            "Node packages verification failed",
            f"Execution error: {e}"
        )


def check_archive_tools() -> Tuple[bool, str, str]:
    tools_found: List[str] = []

    for bin_name in ["7z", "7za", "7zr"]:
        bin_path = shutil.which(bin_name)
        if bin_path:
            try:
                res = subprocess.run([bin_path], capture_output=True, text=True)
                if "7-Zip" in res.stdout or "p7zip" in res.stdout or res.returncode in (0, 7):
                    tools_found.append(f"{bin_name} ({bin_path})")
                    break
            except Exception:
                pass

    unrar_bin = shutil.which("unrar")
    if unrar_bin:
        try:
            res = subprocess.run([unrar_bin], capture_output=True, text=True)
            if "UNRAR" in res.stdout or res.returncode in (0, 7):
                tools_found.append(f"unrar ({unrar_bin})")
        except Exception:
            pass

    if tools_found:
        return True, f"Archive extractor: {', '.join(tools_found)}", ""

    return (
        False,
        "No RAR/7z extractor found",
        "Required for archive inspection. macOS: `brew install sevenzip` | Linux: `apt-get install p7zip-full unrar-free`"
    )


def check_directories() -> Tuple[bool, str, str]:
    required_dirs = ["previews", "scrapes", "metadata"]
    issues = []

    for d in required_dirs:
        dir_path = ROOT_DIR / d
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            test_file = dir_path / ".perm_check"
            test_file.write_text("ok")
            test_file.unlink()
        except Exception as e:
            issues.append(f"'{d}/' unwritable: {e}")

    if not issues:
        return True, "Storage directories writable (previews/, scrapes/, metadata/)", ""

    return False, "Filesystem permission failure", "; ".join(issues)


def main():
    print(f"\n{BOLD}{CYAN}=== DESYNC 3D Vault: Environment & Tooling Verification ==={RESET}")
    print(f"Target Project Root: {ROOT_DIR}\n")

    checks = [
        ("Python Runtime", check_python),
        ("SQLite Engine", check_sqlite),
        ("Node.js Runtime", check_nodejs),
        ("Playwright & Scraper Modules", check_node_packages),
        ("Archive Binaries (7z / unrar)", check_archive_tools),
        ("Directory Permissions", check_directories),
    ]

    all_passed = True
    remediations: List[str] = []

    for label, fn in checks:
        passed, summary, hint = fn()
        if passed:
            print(f"  {GREEN}[PASS]{RESET} {BOLD}{label}:{RESET} {summary}")
        else:
            all_passed = False
            print(f"  {RED}[FAIL]{RESET} {BOLD}{label}:{RESET} {summary}")
            if hint:
                remediations.append(f"{BOLD}{label}:{RESET}\n    -> {hint}")

    print("\n" + "─" * 70)

    if all_passed:
        print(f"{GREEN}{BOLD}All environment checks passed! System is ready for scraping, downloads, and archive peaking.{RESET}\n")
        sys.exit(0)
    else:
        print(f"{RED}{BOLD}Environment verification failed with missing prerequisites:{RESET}\n")
        for r in remediations:
            print(f"  • {r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
