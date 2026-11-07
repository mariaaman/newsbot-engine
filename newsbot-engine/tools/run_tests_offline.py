"""Minimal pytest-free runner for environments without pytest/pydantic (e.g. the build sandbox).
Runs every zero-argument test_* function in tests/test_*.py. Modules that need pytest/pydantic
are reported as SKIPPED, not failed. Use real `pytest` whenever you can."""
import importlib
import pathlib
import sys
import traceback

root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
passed = failed = skipped = 0
for f in sorted((root / "tests").glob("test_*.py")):
    name = f"tests.{f.stem}"
    try:
        mod = importlib.import_module(name)
    except ImportError as e:
        print(f"SKIPPED {name} ({e.name} not installed)")
        skipped += 1
        continue
    for fn_name in sorted(n for n in dir(mod) if n.startswith("test_")):
        fn = getattr(mod, fn_name)
        if fn.__code__.co_argcount:       # needs pytest fixtures (tmp_path etc.)
            print(f"SKIPPED {name}::{fn_name} (needs pytest fixtures)")
            continue
        try:
            fn()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAILED {name}::{fn_name}")
            traceback.print_exc()
print(f"passed={passed} failed={failed} skipped_modules={skipped}")
sys.exit(1 if failed else 0)
