from pathlib import Path

import pydantic  # noqa: F401  (config loading needs it; the offline runner skips this module without it)

from newsbot.cli import main

TEMPLATE = Path(__file__).resolve().parent.parent / "brain-template"


def test_dry_run_passes_offline():
    assert main(["run", "--mode", "dry-run", "--brain", str(TEMPLATE)]) == 0


def test_dry_run_without_brain_falls_back_to_template(tmp_path):
    assert main(["run", "--mode", "dry-run", "--brain", str(tmp_path / "nope")]) == 0


def test_invalid_brain_fails_loudly(tmp_path):
    assert main(["run", "--mode", "review", "--brain", str(tmp_path)]) == 2


def test_validate_command():
    assert main(["validate", "--brain", str(TEMPLATE)]) == 0
