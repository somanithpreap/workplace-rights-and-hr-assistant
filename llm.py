"""
llm.py - the only file that talks to Gemini.

    from llm import generate
    out = generate(system="rules...", prompt="question + sources", purpose="answer")
    out["text"], out["tokens_in"], out["tokens_out"], out["ms"]

Set LLM_MODE=mock in .env to test everything without a key or quota.
"""
import re
import time

import config

ABSTAIN = "The provided sources do not cover this question."


def generate(system: str, prompt: str, purpose: str = "answer", max_tokens: int = None) -> dict:
    t0 = time.time()
    if config.LLM_MODE == "mock":
        text = _mock(prompt, purpose)
        tin, tout = len((system + prompt).split()), len(text.split())   # rough word counts
    else:
        text, tin, tout = _gemini(system, prompt, max_tokens or config.MAX_ANSWER_TOKENS)
    return {"text": text.strip(), "tokens_in": tin, "tokens_out": tout,
            "ms": round((time.time() - t0) * 1000), "model": config.CHAT_MODEL
            if config.LLM_MODE != "mock" else "mock", "purpose": purpose}


_client = None


def _gemini(system, prompt, max_tokens):
    global _client
    from google import genai
    from google.genai import types
    if _client is None:
        _client = genai.Client()            # reads GEMINI_API_KEY from the environment / .env
    cfg = types.GenerateContentConfig(
        system_instruction=system or None, temperature=0.1, max_output_tokens=max_tokens)
    for attempt in range(4):
        try:
            res = _client.models.generate_content(model=config.CHAT_MODEL, contents=prompt, config=cfg)
            usage = res.usage_metadata
            return (res.text or "",
                    getattr(usage, "prompt_token_count", 0) or 0,
                    getattr(usage, "candidates_token_count", 0) or 0)
        except Exception as e:
            msg = str(e)
            if attempt < 3 and any(code in msg for code in ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")):
                wait = 20 * (attempt + 1)
                print(f"[llm] rate limited / busy, waiting {wait}s ...")
                time.sleep(wait)
                continue
            raise


def _mock(prompt, purpose):
    """Predictable fake answers so the team can test routing, tools, guards and traces offline."""
    if purpose == "rewrite":
        prev = re.findall(r"User: (.+)", prompt)
        latest = re.search(r"Latest message: (.+)", prompt)
        base = prev[-1] if prev else ""
        return f"{base} {latest.group(1) if latest else ''}".strip()
    if purpose == "alone":
        return "MOCK (LLM alone): I believe the answer is ... [no sources used]"
    sources = re.findall(r'<source id="(S\d+)" cite="([^"]+)">\s*(.+?)</source>', prompt, re.S)
    tool = re.search(r"<tool_result>\s*(.+?)</tool_result>", prompt, re.S)
    parts = []
    if tool:
        summary = re.search(r'"summary":\s*"([^"]+)"', tool.group(1))
        parts.append(f"MOCK: {summary.group(1) if summary else 'tool result received.'} [tool]")
    if sources:
        sid, cite, text = sources[0]
        first = re.sub(r"\s+", " ", text).strip()[:220]
        parts.append(f"MOCK: {first} {cite}")
    return "\n".join(parts) if parts else ABSTAIN
