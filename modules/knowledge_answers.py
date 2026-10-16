"""Per-tool years and the AI's grounding, from the private knowledge file (2026-10-17, plan section E).

The file C:\\Users\\Admin\\.secrets\\candidate_knowledge.json lives outside every repo; job-tracker's portal_kit/knowledge.py reads it (loaded by path,
like the UAE overlay loads facts.py). This module is the bot's small window onto that code. It never raises into the bot: if the module or the file
cannot be used, every function hands back what the bot would have done without it.
"""
import importlib.util
import os
import threading

KNOWLEDGE_MODULE = r"C:\Users\Admin\GitHub\job-tracker\portal_kit\knowledge.py"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAP_LOG = os.path.join(REPO, "logs", "knowledge_gaps.jsonl")

_K = None
_tried = False
_lock = threading.Lock()


def _knowledge():
    """portal_kit/knowledge.py as a module, loaded once; None when it cannot be loaded."""
    global _K, _tried
    with _lock:
        if not _tried:
            _tried = True
            try:
                spec = importlib.util.spec_from_file_location("portal_knowledge", KNOWLEDGE_MODULE)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _K = mod
            except Exception as e:
                print(f"knowledge file module not usable ({type(e).__name__}: {str(e)[:100]}); the bot's own answers stand.", flush=True)
                _K = None
    return _K


def knowledge_years(label: str, default):
    """A text field asking for years (or months) of experience. With a tool named that the knowledge file has a number for, that number
    (whole years, or whole months when the label asks for months); otherwise `default`, the answer the bot would have typed anyway.
    A tool named without a number is written to logs/knowledge_gaps.jsonl so the number can be supplied."""
    K = _knowledge()
    if K is None:
        return default
    try:
        kind, years, skills = K.years_answer(label)
        if kind == "known":
            return K.format_years(years, label)
        if kind == "unknown":
            K.log_gap(GAP_LOG, label, "years_unknown", skills, "no number for these tools in candidate_knowledge.json; the bot's default was typed")
        elif kind == "none":
            obj = K.named_object(label)
            if obj:
                K.log_gap(GAP_LOG, label, "years_no_key", [obj], "a tool the knowledge file does not list at all; the bot's default was typed")
    except Exception:
        pass
    return default


def grounding_text() -> str:
    """The text an LLM may use to answer about the candidate: the verified facts with the per-tool years, then the resume lines. Empty string
    when the knowledge file cannot be read (the caller then has nothing to ground an answer on and must not ask the model)."""
    K = _knowledge()
    if K is None:
        return ""
    try:
        facts = K.prompt_block()
        if not facts:
            return ""
        resume = K.resume_block()
        return facts + ("\n\n" + resume if resume else "")
    except Exception:
        return ""
