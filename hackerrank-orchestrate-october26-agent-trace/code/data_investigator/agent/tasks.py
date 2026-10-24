"""The LLM's allowed tasks (L1 intent, L2 artifact extraction), each wrapped by the guard."""
from __future__ import annotations

import re
from pathlib import Path

from ..intent import IntentLabel
from .client import ModelUnavailable
from .guard import GuardLog, check_artifact_rows, check_intent

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(name: str) -> tuple[str, str]:
    text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    m = re.search(r"^version:\s*(\S+)", text, re.M)
    body = text.split("---", 2)[-1].strip() if text.startswith("---") else text
    return (m.group(1) if m else name), body


INTENT_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": ["DELETION", "ACCESS", "EXPORT", "CORRECTION", "OPT_OUT", "OTHER"]},
                   "quote": {"type": "string"}},
    "required": ["intent", "quote"],
}

ARTIFACT_SCHEMA = {
    "type": "object",
    "properties": {
        "screen_title": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object", "properties": {
            "resource_id": {"type": "string"}, "identifiers": {"type": "array", "items": {"type": "string"}},
            "status_text": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["resource_id", "identifiers", "status_text", "quote"]}},
    },
    "required": ["rows"],
}

# The heuristic's NOT-deletion decision rests on explicit "keep / do not delete" statements, so
# the model may only flip a heuristic label when its grounded quote supports the flip.
_KEEP_HINT = re.compile(r"keep|leave|don'?t|do not|not asking|not a request|not requesting|continue|still|only|without|no (?:borr|elimin)|não", re.I)


class LLMIntentClassifier:
    def __init__(self, client, log: GuardLog):
        self.client = client
        self.log = log
        self.version, self.system = load_prompt("classify_request.md")

    def classify(self, text: str, heuristic: IntentLabel) -> IntentLabel:
        try:
            out, rec = self.client.generate_json(task="classify_request", prompt_version=self.version,
                                                 system=self.system, user=f"<request>{text}</request>", schema=INTENT_SCHEMA)
        except ModelUnavailable as e:
            self.log.record("classify_request", "fallback:model_unavailable", {"error": str(e)[:80]})
            return heuristic
        intent, quote, verdict = check_intent(out, text)
        rec.verdict = verdict
        if intent is None:
            self.log.record("classify_request", verdict, {"heuristic": heuristic.intent})
            return heuristic
        agree = (intent == "DELETION") == heuristic.is_deletion
        if agree:
            self.log.record("classify_request", "accepted:agree", {"intent": intent, "heuristic": heuristic.intent})
            return IntentLabel(intent if not heuristic.is_deletion else "DELETION", "llm+heuristic", quote)
        # Disagreement: accept the model only when its quote carries an explicit cue for its label.
        if intent != "DELETION" and _KEEP_HINT.search(quote):
            self.log.record("classify_request", "accepted:override", {"intent": intent, "heuristic": heuristic.intent, "quote": quote})
            return IntentLabel(intent, "llm", quote, f"overrode heuristic {heuristic.intent}")
        self.log.record("classify_request", "rejected:disagreement_unsupported",
                        {"intent": intent, "heuristic": heuristic.intent, "quote": quote})
        return IntentLabel(heuristic.intent, "heuristic", heuristic.quote, f"llm said {intent}; kept heuristic")


class ImageExtractor:
    def __init__(self, client, log: GuardLog):
        self.client = client
        self.log = log
        self.version, self.system = load_prompt("extract_artifact.md")

    def extract(self, file_id: str, path: Path, grounded: set[str], known_identifiers: set,
                aliases: dict[str, str] | None = None):
        """Two crops per screenshot (title + main table, right-hand detail panel) so small text is
        read at a usable resolution; rows from both crops are merged by resource id."""
        tiles = _tiles(path, self.client)
        merged: dict[str, dict] = {}
        title = ""
        for tile in tiles:
            try:
                out, rec = self.client.generate_json(
                    task="extract_artifact", prompt_version=self.version, system=self.system,
                    user="Transcribe the records in this screenshot as JSON.", schema=ARTIFACT_SCHEMA, images=[tile])
            except ModelUnavailable as e:
                self.log.record("extract_artifact", "fallback:model_unavailable", {"file_id": file_id, "error": str(e)[:80]})
                return None
            if not isinstance(out, dict):
                rec.verdict = "rejected:schema"
                continue
            title = title or str(out.get("screen_title", ""))
            for row in out.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("resource_id", "")).strip().lower().replace(" ", "_")
                m = merged.setdefault(rid, {"resource_id": rid, "identifiers": [], "status_text": "", "quote": ""})
                m["identifiers"] += [str(x) for x in (row.get("identifiers") or [])]
                m["status_text"] = (m["status_text"] + " " + str(row.get("status_text", ""))).strip()
                m["quote"] = m["quote"] or str(row.get("quote", ""))
            rec.verdict = "accepted"
        rows = check_artifact_rows({"rows": list(merged.values())}, grounded, known_identifiers, self.log, file_id, aliases)
        return rows, title


def _tiles(path: Path, client) -> list[Path]:
    """Crop the screenshot into the regions that carry record data. Crops are cached next to the
    LLM cache so the cache key (image bytes) stays stable across runs."""
    from PIL import Image

    out_dir = Path(getattr(client, "cache_dir", Path("cache/llm"))) / "tiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    im = Image.open(path)
    w, h = im.size
    boxes = {"main": (int(w * 0.12), 0, int(w * 0.79), int(h * 0.55)), "detail": (int(w * 0.78), int(h * 0.24), w, int(h * 0.78))}
    tiles = []
    for name, box in boxes.items():
        p = out_dir / f"{path.stem}.{name}.png"
        if not p.exists():
            im.crop(box).save(p, format="PNG")
        tiles.append(p)
    return tiles
