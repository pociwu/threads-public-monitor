from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def find_bash() -> str | None:
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
    ]
    return next(
        (candidate for candidate in candidates if candidate and Path(candidate).is_file()), None
    )


def test_env_example_can_be_sourced_by_install_scripts() -> None:
    bash = find_bash()
    if bash is None:
        pytest.skip("Bash is not available")
    result = subprocess.run(
        [
            bash,
            "-c",
            'set -eu; source .env.example; test "$APP_NAME" = "Threads Public Monitor"',
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_env_reader_handles_existing_unquoted_values() -> None:
    bash = find_bash()
    if bash is None:
        pytest.skip("Bash is not available")
    project_root = Path(__file__).parents[1]
    env_file = project_root / ".test-legacy.env"
    env_file.write_text("APP_NAME=Threads Public Monitor\nWEB_PORT=8080\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [
                bash,
                "-c",
                "source scripts/env.sh; read_env_value .test-legacy.env APP_NAME",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "Threads Public Monitor"
    finally:
        env_file.unlink(missing_ok=True)
