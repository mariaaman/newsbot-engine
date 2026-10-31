import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pending_triage as pt      # noqa: E402


def _draft(path: Path, title: str, body: str, pending: str = "", lang: str = "en") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\nlang: {lang}\n{pending}---\n{body}\n", encoding="utf-8")


def test_word_count_ignores_headings_and_markers():
    assert pt.count_words("## Head\nOne two three [S1][S2] four.\n(S3) five") == 5


def test_triage_buckets():
    with tempfile.TemporaryDirectory() as t:
        b = Path(t)
        (b / "config").mkdir()
        (b / "config" / "newsbot.yaml").write_text("qa: {min_words: {en: 5}}\n", encoding="utf-8")
        long = "alpha beta gamma delta epsilon zeta eta theta"
        _draft(b / "content/en/live.md", "Metro line extension opens downtown", long)
        _draft(b / "pending/en/stale.md", "River flood warning issued upstream", long, "pending:\n  failures: [min_words]\n  review: []\n")
        _draft(b / "pending/en/hard.md", "Central bank raises interest rate", long, "pending:\n  failures: [unsupported_name]\n  review: []\n")
        _draft(b / "pending/en/short.md", "Harbour dredging contract awarded", "too short", "pending:\n  failures: [min_words]\n  review: []\n")
        _draft(b / "pending/en/sens.md", "Wildfire season starts early", long, "pending:\n  failures: []\n  review: [sensitive:death]\n")
        _draft(b / "pending/en/dup.md", "Metro line extension opens downtown today", long, "pending:\n  failures: []\n  review: []\n")
        out = pt.triage(b)
        names = {k: sorted(n.split("/")[-1] for n, _ in v) for k, v in out.items()}
        assert names["recheck_needed"] == ["stale.md"]
        assert names["rewrite_or_drop"] == ["hard.md", "short.md"]
        assert names["read_and_approve"] == ["sens.md"]
        assert names["duplicate_of"] == ["dup.md"]
