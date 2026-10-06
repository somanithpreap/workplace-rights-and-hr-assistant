import json
import os
import re
from typing import List, Dict, Any
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
KNOWLEDGE_BASE_PATH = "knowledge_base.json"

def load_knowledge_base() -> List[Dict[str, Any]]:
    """Loads indexed chunks from knowledge_base.json."""
    if not os.path.exists(KNOWLEDGE_BASE_PATH):
        raise FileNotFoundError(
            f"'{KNOWLEDGE_BASE_PATH}' not found. Run 'python ingest.py' first."
        )
    with open(KNOWLEDGE_BASE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def search(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Searches knowledge_base.json for chunks matching the user query.
    Performs keyword & article-number relevance scoring across legal documents.
    """
    kb = load_knowledge_base()
    query_words = set(re.findall(r'\w+', query.lower()))
    
    results = []
    for item in kb:
        text = item.get("text", "").lower()
        title = item.get("title", "").lower()
        
        # Calculate term overlap score
        score = sum(1 for word in query_words if word in text or word in title)
        
        if score > 0:
            results.append({
                "score": score,
                "source": item.get("source", "Unknown Document"),
                "article": item.get("article", "N/A"),
                "page": item.get("page", "N/A"),
                "text": item.get("text", "")
            })
            
    # Sort by relevance score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]

def format_rag_context(results: List[Dict[str, Any]]) -> str:
    """Formats retrieved chunks into context string with article citations for LLM context."""
    if not results:
        return "No relevant legal articles or company rules found."
    
    formatted = []
    for res in results:
        citation = f"[{res['source']} Art. {res['article']}, p.{res['page']}]"
        formatted.append(f"Source {citation}:\n{res['text']}\n")
        
    return "\n---\n".join(formatted)

if __name__ == "__main__":
    test_query = "overtime pay rate Sunday"
    print(f"--- Testing RAG Search for: '{test_query}' ---")
    matches = search(test_query, top_k=2)
    print(format_rag_context(matches))