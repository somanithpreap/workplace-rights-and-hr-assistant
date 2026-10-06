"""
guard.py - the three guardrail layers that live in code (the 4th layer is the safe system prompt in answer.py).

  check_input(message, session)   -> block attacks BEFORE anything else runs
  clean_untrusted(text)           -> remove instruction-like text from cards and tool results
  check_output(answer, ...)       -> no prompt leak, no other people's data, every answer cited

Each check returns which rule fired, so the trace and the attack table can show "which defence caught it".
"""
import re

import tools

CANARY = "HRA-CANARY-7Q2X"     # hidden marker inside the system prompt; if it ever appears in an answer = leak

REFUSALS = {
    "direct_injection": "I can't change my rules. I can help with questions about leave, wages, overtime, "
                        "seniority indemnity and social security.",
    "prompt_leak": "I can't share my internal instructions. Ask me about your workplace rights instead.",
    "roleplay": "I can't play a role that bypasses my rules. I'm happy to answer HR questions.",
    "sql": "That looks like a database command. I only answer questions in plain language.",
    "harmful": "I can't help with that. If you were sick, the internal rules explain how to submit a real "
               "medical certificate (section 5.2).",
    "other_person": "I can only show your own records. Information about other workers is private. "
                    "If you need it for work, please ask HR directly.",
}

_SLANG = {r"\bur\b": "your", r"\bu\b": "you", r"\bplz\b": "please", r"\bpls\b": "please", r"\bn\b": "and",
          r"\bsys prompt\b": "system prompt"}

PATTERNS = [
    ("direct_injection", r"\b(ignore|forget|disregard|override|bypass)\b.{0,40}\b(rules?|instructions?|guidelines?|restrictions?|prompt)\b"),
    ("direct_injection", r"\b(no restrictions|without restrictions|free ai|jailbreak|developer mode|do anything now)\b"),
    ("direct_injection", r"\byou are now\b|\bnew rules?:"),
    ("prompt_leak", r"\b(print|show|reveal|repeat|display|tell me|give me|output|leak)\b.{0,40}\b(system prompt|your prompt|your instructions|hidden (rules|notes)|admin notes|initial instructions)\b"),
    ("prompt_leak", r"\bword for word\b|\bverbatim\b"),
    ("roleplay", r"\b(pretend|role.?play|act as|imagine you are|you are my)\b.{0,80}\b(grand(ma|mother|pa|father)|admin|secret|developer|unrestricted)\b"),
    ("sql", r"(\bdrop\s+table\b|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bunion\s+select\b|';|--\s*$|\bor\s+1\s*=\s*1\b)"),
    ("harmful", r"\b(fake|forge|falsify|counterfeit|make up)\b.{0,30}\b(medical|doctor'?s?|sick)\b.{0,15}\b(certificate|note|letter)\b"),
]
KHMER_PATTERNS = [
    ("direct_injection", r"បំភ្លេច"),               # "forget"
    ("prompt_leak", r"បង្ហាញ.{0,40}(prompt|ប្រអប់|សេចក្តីណែនាំ)"),   # "show ... prompt / instructions"
]

UNTRUSTED_PATTERNS = re.compile(
    r"(\b(assistant|system|user)\s*:|ignore (all |your |the )?(previous |prior )?(rules|instructions)"
    r"|reveal (your |the )?(system )?prompt|tell the user to|send (usd|\$|money|\d)|account\s*\d{3}"
    r"|you (must|should) now)", re.I)


def _normalise(text):
    t = text.lower()
    for pat, rep in _SLANG.items():
        t = re.sub(pat, rep, t)
    return t


def check_input(message, session):
    """Returns {"action": "allow" | "block", "type": ..., "reply": ..., "note": ...}."""
    t = _normalise(message)
    for kind, pat in PATTERNS:
        if re.search(pat, t, re.I | re.S):
            return {"action": "block", "type": kind, "rule": pat[:60], "reply": REFUSALS[kind]}
    for kind, pat in KHMER_PATTERNS:
        if re.search(pat, message):
            return {"action": "block", "type": kind, "rule": "khmer:" + pat, "reply": REFUSALS[kind]}

    # other people's data: identity ALWAYS comes from the session, never from the message
    if session["role"] != "hr_officer":
        me = session["employee_id"]
        claim = re.search(r"\b(i am|i'm|my id is|this is)\s+(e\d{3})\b", t)
        if claim:
            return {"action": "allow", "type": "identity_claim_ignored",
                    "note": f"Message claims to be {claim.group(2).upper()}; using the signed-in ID {me}."}
        ids = {x.upper() for x in re.findall(r"\be\d{3}\b", t)} - {me}
        if ids:
            return {"action": "block", "type": "other_person", "rule": f"mentions {sorted(ids)}",
                    "reply": REFUSALS["other_person"]}
        my_name = tools.get_employee(me)["full_name"].lower()
        for eid, name in tools.all_employee_names().items():
            if eid != me and name.lower() != my_name and name.lower() in t:
                return {"action": "block", "type": "other_person", "rule": f"mentions {name}",
                        "reply": REFUSALS["other_person"]}
    return {"action": "allow", "type": "clean", "note": ""}


def clean_untrusted(text):
    """Cards and tool results are DATA. Remove any line that tries to give instructions."""
    if not text:
        return text, False
    flagged = False
    out = []
    for line in str(text).split("\n"):
        if UNTRUSTED_PATTERNS.search(line):
            flagged = True
            out.append("[removed: instruction-like text found in data]")
        else:
            out.append(line)
    return "\n".join(out), flagged


def check_output(answer, session, allowed_citations, needs_citation):
    """Returns {"ok": bool, "problems": [...]}."""
    problems = []
    if CANARY in answer or "RULES FOR THE HR ASSISTANT" in answer.upper():
        problems.append("system_prompt_leak")
    if session["role"] != "hr_officer":
        me = session["employee_id"]
        for eid, name in tools.all_employee_names().items():
            if eid != me and (name in answer or re.search(rf"\b{eid}\b", answer)):
                problems.append(f"other_person_data:{eid}")
    if needs_citation and "do not cover this question" not in answer:
        if not any(c and c in answer for c in allowed_citations) and "[" not in answer:
            problems.append("missing_citation")
    return {"ok": not problems, "problems": problems}
