"""Tier 4 End-to-End CLI Simulation Test Suite for Milestone M5.

Tests command-line interface execution of `scripts/simulate_game.py` across:
- Automated headless simulation (--auto) with default catalog quote.
- Specific quote index selection (--auto --quote <idx>).
- Custom Cyrillic plaintext cryptograms (--auto --custom <text>).
- Deterministic random seed reproducibility (--auto --seed <int>).
- Argument validation and pathological error exits (--quote 999, non-Russian --custom).
- Interactive mode stdin EOF handling without unhandled exceptions.
- Command-line help, db selection, and process exit codes.
"""
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_cli(
    args: List[str],
    input_str: Optional[str] = None,
    timeout: float = 25.0,
) -> subprocess.CompletedProcess:
    """Helper to execute scripts.simulate_game as a standalone subprocess."""
    cmd = [sys.executable, "-m", "scripts.simulate_game", *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    return subprocess.run(
        cmd,
        input=input_str,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=timeout,
    )


# ==============================================================================
# Tier 4: Automated Simulation Tests (--auto)
# ==============================================================================


@pytest.mark.e2e
class TestSimulationCliAutoMode:
    """End-to-end CLI tests for automated simulation execution."""

    def test_cli_auto_default_quote(self):
        """CLI: --auto with default catalog quote runs to victory with exit code 0."""
        result = run_cli(["--auto", "--no-color"])
        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}.\n"
            f"STDERR: {result.stderr}\nSTDOUT: {result.stdout}"
        )
        assert "Traceback (most recent call last)" not in result.stderr

        stdout_lower = result.stdout.lower()
        assert any(
            kw in stdout_lower
            for kw in ["победа", "victory", "успешно", "расшифрован", "завершена"]
        ), "Victory banner not found in output"

    def test_cli_auto_specific_quote(self):
        """CLI: --auto --quote 5 selects quote 5 (Sorge/Ramzay) and completes with exit code 0."""
        result = run_cli(["--auto", "--quote", "5", "--no-color"])
        assert result.returncode == 0, f"Quote 5 run failed: {result.stderr}"
        assert "Traceback (most recent call last)" not in result.stderr

        stdout_lower = result.stdout.lower()
        assert any(
            kw in stdout_lower
            for kw in ["рамзай", "сводку", "подтверждения", "победа", "успешно"]
        ), f"Expected quote 5 content in output:\n{result.stdout}"

    def test_cli_auto_custom_russian_text(self):
        """CLI: --auto --custom decrypted custom Russian text and completes with exit code 0."""
        phrase = "СЕКРЕТНЫЙ ПРИКАЗ КОМАНДОВАНИЯ"
        result = run_cli(["--auto", "--custom", phrase, "--no-color"])
        assert result.returncode == 0, f"Custom text run failed: {result.stderr}"
        assert "Traceback (most recent call last)" not in result.stderr

        assert phrase in result.stdout or phrase.lower() in result.stdout.lower()
        stdout_lower = result.stdout.lower()
        assert any(
            kw in stdout_lower
            for kw in ["победа", "victory", "успешно", "завершена"]
        )

    def test_cli_auto_deterministic_seed(self):
        """CLI: --auto --seed 12345 produces deterministic, reproducible output."""
        seed_arg = "12345"
        res1 = run_cli(["--auto", "--quote", "0", "--seed", seed_arg, "--no-color"])
        res2 = run_cli(["--auto", "--quote", "0", "--seed", seed_arg, "--no-color"])

        assert res1.returncode == 0
        assert res2.returncode == 0
        assert "Traceback (most recent call last)" not in res1.stderr
        assert "Traceback (most recent call last)" not in res2.stderr

        import re
        strip_var = lambda s: re.sub(r"game_[a-f0-9]+|Время=[\d\.]+с", "", s)
        assert strip_var(res1.stdout) == strip_var(res2.stdout)

    def test_cli_auto_file_database(self, tmp_path):
        """CLI: --auto --db <path> creates SQLite file on disk and completes cleanly."""
        db_path = tmp_path / "cli_sim_test.db"
        result = run_cli(
            ["--auto", "--quote", "1", "--db", str(db_path), "--no-color"]
        )
        assert result.returncode == 0, f"File DB run failed: {result.stderr}"
        assert db_path.exists(), "Database file was not created on disk"
        assert db_path.stat().st_size > 0, "Database file is empty"


# ==============================================================================
# Tier 4: Argument Validation & Boundary Error Tests
# ==============================================================================


@pytest.mark.e2e
class TestSimulationCliValidationErrors:
    """Validates CLI argument errors report informative messages and exit cleanly."""

    @pytest.mark.parametrize("invalid_quote", ["999", "-1", "20", "100"])
    def test_invalid_quote_index_rejected(self, invalid_quote):
        """CLI rejects out-of-range quote index with error code and no traceback."""
        result = run_cli(["--auto", "--quote", invalid_quote])
        assert result.returncode != 0, f"Expected non-zero exit for quote {invalid_quote}"
        assert "Traceback (most recent call last)" not in result.stderr

        err_output = (result.stderr + result.stdout).lower()
        assert any(
            kw in err_output
            for kw in ["quote", "индекс", "диапазон", "0", "19", "range", "error", "invalid"]
        ), f"Missing informative error message in: {result.stderr}"

    @pytest.mark.parametrize(
        "invalid_text",
        [
            "abc",
            "Hello World 123",
            "1234567890",
            "!!! ??? --",
        ],
    )
    def test_invalid_custom_text_non_russian_rejected(self, invalid_text):
        """CLI rejects non-Russian or symbol-only custom text with error code and no traceback."""
        result = run_cli(["--auto", "--custom", invalid_text])
        assert result.returncode != 0, f"Expected non-zero exit for text: {invalid_text}"
        assert "Traceback (most recent call last)" not in result.stderr

        err_output = (result.stderr + result.stdout).lower()
        assert any(
            kw in err_output
            for kw in ["русск", "кириллиц", "cyrillic", "букв", "invalid", "error", "ошибка"]
        ), f"Missing informative validation error message in: {result.stderr}"

    def test_mutually_exclusive_quote_and_custom(self):
        """CLI rejects specifying both --quote and --custom simultaneously."""
        result = run_cli(["--auto", "--quote", "2", "--custom", "ПРИКАЗ ШТАБА"])
        assert result.returncode != 0
        assert "Traceback (most recent call last)" not in result.stderr

    def test_cli_help_flag(self):
        """CLI: --help prints command-line options and exits with code 0."""
        result = run_cli(["--help"])
        assert result.returncode == 0
        assert "--auto" in result.stdout
        assert "--interactive" in result.stdout
        assert "--quote" in result.stdout
        assert "--custom" in result.stdout
        assert "--seed" in result.stdout


# ==============================================================================
# Tier 4: Interactive Mode & EOF Robustness Tests
# ==============================================================================


@pytest.mark.e2e
class TestSimulationCliInteractiveMode:
    """Verifies interactive CLI mode handling of stdin streams and EOF."""

    def test_interactive_stdin_eof_graceful_exit(self):
        """Simulated EOF on closed stdin terminates cleanly without unhandled EOFError."""
        result = run_cli(["--interactive", "--no-color"], input_str="")

        assert "Traceback (most recent call last)" not in result.stderr
        assert "EOFError" not in result.stderr
        assert result.returncode in (0, 1), (
            f"Unexpected exit code {result.returncode} on EOF. Stderr: {result.stderr}"
        )

        output = (result.stdout + result.stderr).lower()
        assert any(
            kw in output
            for kw in ["eof", "выход", "завершен", "прерван", "сеанс", "goodbye", "до свидания"]
        ), f"Missing graceful exit message in output:\n{result.stdout}\n{result.stderr}"

    def test_interactive_quit_command(self):
        """Interactive mode exits cleanly when user inputs 'Q' or 'q'."""
        result = run_cli(["--interactive", "--no-color"], input_str="q\n")
        assert result.returncode == 0
        assert "Traceback (most recent call last)" not in result.stderr

    def test_interactive_dossier_view(self):
        """Interactive mode option 5 opens Analyst Dossier and returns cleanly."""
        result = run_cli(["--interactive", "--no-color"], input_str="3\n5\n\nq\n")
        assert result.returncode == 0
        assert "Traceback (most recent call last)" not in result.stderr
        assert any(
            kw in result.stdout.lower()
            for kw in ["досье", "операция", "источник"]
        )

    def test_interactive_guess_symbol(self):
        """Interactive mode option 1 submits guess without attribute errors."""
        result = run_cli(["--interactive", "--no-color"], input_str="3\n1\n2\nП\nq\n")
        assert result.returncode == 0
        assert "Traceback (most recent call last)" not in result.stderr
        assert any(
            kw in result.stdout.lower()
            for kw in ["символ", "подтвержден", "ошибка"]
        )


# ==============================================================================
# Tier 4: Programmatic Execution Tests
# ==============================================================================


@pytest.mark.e2e
class TestSimulationCliProgrammatic:
    """Direct in-process execution tests for scripts.simulate_game.main."""

    def test_programmatic_main_auto(self):
        """Invoking main() directly with --auto arguments returns 0."""
        from scripts.simulate_game import main

        exit_code = main(["--auto", "--quote", "0", "--no-color"])
        assert exit_code == 0
