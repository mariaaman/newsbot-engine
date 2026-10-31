"""Prompt loading (brain/prompts/<name>.md overrides engine brain-template) and prompt-injection defence:
untrusted text goes only inside marked data blocks that cannot be closed from the inside."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "brain-template" / "prompts"


def load_prompt(brain: str | Path | None, name: str) -> str:
    for base in ((Path(brain) / "prompts") if brain else None, TEMPLATE):
        if base and (base / f"{name}.md").exists():
            return re.sub(r"<!--.*?-->\s*", "", (base / f"{name}.md").read_text(encoding="utf-8"), flags=re.S).strip()
    raise FileNotFoundError(f"prompt not found: {name}")


def prompt_version(brain: str | Path | None, name: str) -> str:
    for base in ((Path(brain) / "prompts") if brain else None, TEMPLATE):
        if base and (base / f"{name}.md").exists():
            m = re.search(r"<!--\s*version:\s*(\S+)\s*-->", (base / f"{name}.md").read_text(encoding="utf-8"))
            return m.group(1) if m else "0"
    return "0"


def _attr(v: str) -> str:
    return re.sub(r'[^\w .@:/-]', "", str(v))[:80]


def wrap_untrusted(tag: str, text: str, **attrs: str) -> str:
    """<tag a="b">text</tag> where any tag-like markup inside text is defanged, so a source cannot
    close the block and smuggle instructions outside it."""
    safe = re.sub(r"<\s*(/?)\s*(source_data|claims_data|fact_sheet|draft_data)", r"&lt;\1\2", text, flags=re.I)
    a = "".join(f' {k}="{_attr(v)}"' for k, v in attrs.items())
    return f"<{tag}{a}>\n{safe}\n</{tag}>"


# Layer 1 of the injection defence: drop sentences that address the model instead of reporting news.
# Heuristic on purpose (later layers do not rely on it): it removes the obvious cases cheaply and may
# drop a rare genuine sentence, which only costs a little source text.
_INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|preceding)|system prompt|"
    r"\byou are (now |an? )|\bas an ai\b|new instructions|do not (mention|tell)|"
    r"^\W*(also |please |now |then )?(write|state|say|add|insert|include|output|reply)\s+(that|a link|this|the following)\b|"
    r"\bfrom now on\b[^.]{0,40}\b(answer|respond|reply|write|say|act|behave|ignore|always|never|you|use)\b|"
    r"^\W*(always|never)\s+(mention|say|include|recommend|write|add|use|cite|link|promote|advertise)\b|"
    r"\b(include|insert|add|append|prepend|embed)\s+(the\s+|a\s+|this\s+)?(word|phrase|text|string|link|url|sentence)\b|"
    r"^\W*(you\s+(must|should|will|shall)|your\s+(task|job|goal|instructions?)|make\s+sure\s+(to|you)|"
    r"be\s+sure\s+to|remember\s+to|(respond|reply|answer)\s+(only|with|in))\b|"
    r"^\W*\[?(system|assistant|developer|user)\]?\s*:|<\s*/?\s*(system|assistant|instructions?)\b|\[/?INST\]|<\|\w+\|>|"
    r"\bwhen\s+(you\s+)?(summar\w+|writ\w+|rewrit\w+|translat\w+|repl\w+)\b.{0,80}\b(include|mention|add|insert|say|state)\b|"
    r"নির্দেশ\w*\s*(উপেক্ষা|অগ্রাহ্য)",
    re.I)


def _fold(s: str) -> str:
    """Match copy: NFKC and no invisible format characters (zero-width spaces used to split trigger words)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", s) if unicodedata.category(ch) != "Cf")


def strip_injection(text: str) -> tuple[str, int]:
    """Returns (clean_text, number_of_sentences_removed). Counts only; removed text is never logged."""
    parts = re.split(r"(?<=[.!?\u0964])\s+|\n+", text)
    keep = [s for s in parts if not _INJECTION.search(_fold(s))]
    return "\n".join(keep), len(parts) - len(keep)
