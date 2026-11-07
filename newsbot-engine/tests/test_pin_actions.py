import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pin_actions as pa      # noqa: E402

SHA1, SHA2, SHA3 = "a" * 40, "b" * 40, "c" * 40


def fake_api(path):
    if path == "repos/actions/checkout/git/ref/tags/v4":
        return {"object": {"type": "commit", "sha": SHA1}}
    if path == "repos/peaceiris/actions-hugo/git/ref/tags/v3":                 # annotated tag
        return {"object": {"type": "tag", "sha": SHA2}}
    if path == f"repos/peaceiris/actions-hugo/git/tags/{SHA2}":
        return {"object": {"type": "commit", "sha": SHA3}}
    raise AssertionError(path)


def test_pins_tags_follows_annotated_tags_and_skips_pinned_and_local():
    src = ("steps:\n"
           "      - uses: actions/checkout@v4                      # VERIFY: pin to a commit SHA before going live\n"
           "        with:\n          persist-credentials: false\n"
           "      - uses: peaceiris/actions-hugo@v3\n"
           f"      - uses: actions/cache@{SHA1}  # v4\n"
           "      - uses: ./local-action\n")
    new, changes = pa.pin_text(src, api=fake_api)
    assert f"actions/checkout@{SHA1}  # v4\n" in new and "VERIFY" not in new
    assert f"peaceiris/actions-hugo@{SHA3}  # v3" in new
    assert f"actions/cache@{SHA1}  # v4\n" in new and "uses: ./local-action\n" in new
    assert len(changes) == 2 and "persist-credentials: false" in new
