"""
search.py - find the law "cards" that best answer a question.

Three ways to search (you can compare them for the Section 1 slides):
  bm25    keyword search - good for exact words like "Article 162" or "Prakas 442"
  dense   meaning search with Gemini embeddings - good for paraphrases
  hybrid  both lists combined (Reciprocal Rank Fusion) - the default

Run once to build the vectors (needs GEMINI_API_KEY in .env):
    python search.py --build

Try a question:
    python search.py "What happens if a public holiday falls on a Sunday?"
    python search.py "overtime on Sunday" --mode bm25 --k 5

Use from other code:
    from search import search
    results = search("question", k=5)
    for r in results:
        print(r["citation"], r["score"], r["text"][:80])

If vectors.npy does not exist yet, hybrid falls back to bm25 and prints a warning.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROCESSED = HERE / "data" / "processed"
KB_PATH = Path(os.getenv("KB_PATH", PROCESSED / "knowledge_base.jsonl"))
VEC_PATH = PROCESSED / "vectors.npy"
VEC_META_PATH = PROCESSED / "vectors_meta.json"
QUERY_CACHE_PATH = PROCESSED / "query_cache.json"

# Check the exact embedding model name in AI Studio and change it here or in .env
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")
EMBED_BATCH = 50      # cards per API request
EMBED_PAUSE = 4.5     # seconds between requests, to stay under the per-minute limit

# --------------------------------------------------------------------------
# Hidden trap in the data: these 1997 articles are no longer the law.
# (Better long-term home: a "status" field inside knowledge_base.jsonl.)
# --------------------------------------------------------------------------
SUPERSEDED = {
    "labour_law:article_089": (
        "SUPERSEDED: the 2018 amendment replaced this dismissal indemnity (capped at 6 months) "
        "with seniority indemnity, paid in June and December with no maximum (Prakas 443)."
    ),
}
for _n in range(104, 110):
    SUPERSEDED[f"labour_law:article_{_n:03d}"] = (
        "ABROGATED: Articles 104-109 were cancelled by Article 29 of the Law on Minimum Wage (2018)."
    )

SHORT_NAMES = {
    "Labour Law 1997": "Labour Law",
    "Law on Social Security Schemes 2019": "Social Security Law",
    "Minimum Wage Law 2018": "Minimum Wage Law",
    "Prakas No. 442 on Payment of Wages 2018": "Prakas 442",
    "Seniority Indemnity Guidance 2018": "MLVT Seniority Guidance",
    "Mekong Apparel Internal Work Rules": "Internal Work Rules",
}

STOPWORDS = set("""a an the of to in on for and or is are was were be by at as it its this that
with from what how when who which do does did can i my me we our you your there their if
will would should any all about into than then so not no""".split())


# --------------------------------------------------------------------------
# 1. Load the cards
# --------------------------------------------------------------------------
def make_citation(card):
    """Turn a card into a short label like [Labour Law Art. 162, p.29]."""
    doc = SHORT_NAMES.get(card["document"], card["document"])
    unit = card["id"].split(":", 1)[1]
    if unit.startswith("article_"):
        label = f"Art. {int(unit.split('_')[1])}"
    elif card["document"].startswith("Mekong"):
        label = "§" + card["title"]
    else:
        label = card.get("section") or card["title"]
    page = card.get("source_page")
    return f"[{doc} {label}" + (f", p.{page}" if page else "") + "]"


def load_cards(path=KB_PATH):
    cards = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            card = json.loads(line)
            body = re.sub(r"^#+.*$", "", card["text"], flags=re.M).strip()
            if len(body) < 20:          # skip empty cards (e.g. a heading with no text)
                continue
            card["citation"] = make_citation(card)
            card["note"] = SUPERSEDED.get(card["id"], "")
            cards.append(card)
    return cards


CARDS = load_cards()


# --------------------------------------------------------------------------
# 2. Keyword search (BM25)
# --------------------------------------------------------------------------
def tokenize(text):
    text = text.lower().replace("art.", "article ")
    return [w for w in re.findall(r"[a-z0-9]+", text) if w not in STOPWORDS]


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / len(self.lengths)
        df = Counter(word for d in self.docs for word in d)
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def scores(self, query):
        q = tokenize(query)
        out = np.zeros(len(self.docs), dtype=np.float32)
        for i, (d, length) in enumerate(zip(self.docs, self.lengths)):
            s = 0.0
            for w in q:
                tf = d.get(w, 0)
                if tf:
                    s += self.idf[w] * tf * (self.k1 + 1) / (
                        tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
            out[i] = s
        return out


def card_search_text(card):
    return f"{card['document']} {card['citation']} {card.get('section') or ''}\n{card['text']}"


BM25_INDEX = BM25([card_search_text(c) for c in CARDS])


# --------------------------------------------------------------------------
# 3. Meaning search (Gemini embeddings)
# --------------------------------------------------------------------------
def _gemini_client():
    from dotenv import load_dotenv
    from google import genai
    load_dotenv()
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is missing. Put it in your .env file.")
    return genai.Client()


def embed(texts, task_type, retries=4):
    """task_type: RETRIEVAL_DOCUMENT for cards, RETRIEVAL_QUERY for questions. Never swap them."""
    from google.genai import types
    client = _gemini_client()
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        for attempt in range(retries):
            try:
                res = client.models.embed_content(
                    model=EMBED_MODEL, contents=batch,
                    config=types.EmbedContentConfig(task_type=task_type))
                vectors.extend(e.values for e in res.embeddings)
                break
            except Exception as e:                       # usually 429 = rate limit
                print(f"  embedding error: {str(e)[:160]}")
                if attempt == retries - 1:
                    raise
                print("  waiting 30 s and retrying...")
                time.sleep(30)
        if len(texts) > EMBED_BATCH:
            print(f"  embedded {min(start + EMBED_BATCH, len(texts))}/{len(texts)} cards")
            time.sleep(EMBED_PAUSE)
    v = np.array(vectors, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def kb_fingerprint():
    return hashlib.sha256("".join(c["id"] + c["sha256"] for c in CARDS).encode()).hexdigest()[:16]


def build_vectors():
    print(f"Embedding {len(CARDS)} cards with {EMBED_MODEL} ...")
    t0 = time.time()
    vectors = embed([card_search_text(c) for c in CARDS], "RETRIEVAL_DOCUMENT")
    np.save(VEC_PATH, vectors)
    VEC_META_PATH.write_text(json.dumps({
        "model": EMBED_MODEL, "cards": len(CARDS), "dimensions": int(vectors.shape[1]),
        "kb_fingerprint": kb_fingerprint(), "seconds": round(time.time() - t0, 1),
    }, indent=2))
    print(f"Saved {VEC_PATH.name}: {vectors.shape[0]} vectors x {vectors.shape[1]} dims "
          f"in {time.time() - t0:.0f} s")


def load_vectors():
    if not VEC_PATH.exists():
        return None
    meta = json.loads(VEC_META_PATH.read_text())
    if meta.get("kb_fingerprint") != kb_fingerprint():
        print("WARNING: knowledge base changed since vectors were built. Run: python search.py --build",
              file=sys.stderr)
        return None
    return np.load(VEC_PATH)


VECTORS = load_vectors()


def query_vector(question):
    """Embed the question, with a small cache so test runs don't waste daily requests."""
    cache = json.loads(QUERY_CACHE_PATH.read_text()) if QUERY_CACHE_PATH.exists() else {}
    key = f"{EMBED_MODEL}|{question}"
    if key not in cache:
        cache[key] = embed([question], "RETRIEVAL_QUERY", retries=1)[0].tolist()   # fail fast for live chat
        QUERY_CACHE_PATH.write_text(json.dumps(cache))
    return np.array(cache[key], dtype=np.float32)


# --------------------------------------------------------------------------
# 4. The search function everyone else calls
# --------------------------------------------------------------------------
def _ranking(scores):
    order = np.argsort(-scores)
    ranks = np.empty(len(scores), dtype=int)
    ranks[order] = np.arange(1, len(scores) + 1)
    return order, ranks


def search(question, k=5, mode="hybrid", rrf_k=60):
    """Return the top-k cards as dicts: id, citation, text, note, score, ranks, mode."""
    if mode in ("dense", "hybrid") and VECTORS is None:
        print("WARNING: no vectors yet (run: python search.py --build). Using bm25 only.",
              file=sys.stderr)
        mode = "bm25"

    bm25_scores = BM25_INDEX.scores(question)
    bm25_order, bm25_ranks = _ranking(bm25_scores)

    qvec = None
    if mode != "bm25":
        try:
            qvec = query_vector(question)
        except Exception as e:     # quota / network problem: keyword search still works, so keep answering
            print(f"WARNING: meaning search unavailable ({str(e)[:80]}). Falling back to bm25.", file=sys.stderr)
            mode = "bm25 (fallback: embedding failed)"

    if mode.startswith("bm25"):
        final_order, final_scores = bm25_order, bm25_scores
        dense_scores = dense_ranks = None
    else:
        dense_scores = VECTORS @ qvec                          # cosine similarity
        dense_order, dense_ranks = _ranking(dense_scores)
        if mode == "dense":
            final_order, final_scores = dense_order, dense_scores
        else:                                                  # hybrid: Reciprocal Rank Fusion
            final_scores = 1 / (rrf_k + bm25_ranks) + 1 / (rrf_k + dense_ranks)
            final_order = np.argsort(-final_scores)

    results = []
    for i in final_order[:k]:
        c = CARDS[i]
        results.append({
            "id": c["id"],
            "citation": c["citation"],
            "document": c["document"],
            "source_page": c.get("source_page"),
            "text": c["text"],
            "note": c["note"],
            "score": round(float(final_scores[i]), 4),
            "bm25_score": round(float(bm25_scores[i]), 3),
            "dense_score": round(float(dense_scores[i]), 3) if dense_scores is not None else None,
            "ranks": {
                "bm25": int(bm25_ranks[i]),
                "dense": int(dense_ranks[i]) if dense_ranks is not None else None,
            },
            "mode": mode,
        })
    return results


# --------------------------------------------------------------------------
# 5. Command line
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Search the knowledge base")
    p.add_argument("question", nargs="?")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--mode", choices=["bm25", "dense", "hybrid"], default="hybrid")
    p.add_argument("--build", action="store_true", help="embed all cards and save vectors.npy")
    args = p.parse_args()

    if args.build:
        build_vectors()
        return
    if not args.question:
        p.print_help()
        return

    t0 = time.time()
    results = search(args.question, k=args.k, mode=args.mode)
    ms = (time.time() - t0) * 1000
    print(f"\nQuestion: {args.question}\nMode: {results[0]['mode']}   ({ms:.0f} ms)\n")
    print(f"{'#':<3}{'score':>8}  {'bm25 rank':>9}  {'dense rank':>10}  citation")
    for n, r in enumerate(results, 1):
        dr = r["ranks"]["dense"] if r["ranks"]["dense"] is not None else "-"
        print(f"{n:<3}{r['score']:>8}  {r['ranks']['bm25']:>9}  {dr:>10}  {r['citation']}")
        if r["note"]:
            print(f"   ! {r['note']}")
    print(f"\nTop card text:\n{results[0]['text'][:500]}")


if __name__ == "__main__":
    main()
