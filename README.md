# Mekong Apparel HR Assistant (FECO303 final project, Topic 2)

> Our chatbot helps garment-factory workers and HR officers understand leave, wages, overtime, seniority indemnity
> and social security rights, using the Cambodian Labour Law, the Law on Social Security Schemes, the Law on Minimum
> Wage, Ministry guidance and the factory's internal work rules.

Design: **Level 2 rule router + calculator tools** (see `docs/DESIGN_DECISION_RECORD.md`).

## 1. Setup (each member, once)

```bash
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env   -> then put YOUR OWN key inside
python seed_db.py             # builds data/processed/hr.db from data/raw/csv/*.csv
```

## Folder layout

```
data/
  raw/                      ORIGINAL files, never edited (the "before" for Section 1 slides)
    csv/                    employees, leave_requests, public_holidays_2026, minimum_wage
    laws/                   the 5 law PDFs
    company/                mekong-apparel-internal-work-rules.md
    exchange_rate_usd.json  backup rate if the live API fails
  processed/                what our scripts made (this is what the chatbot reads)
    hr.db                   <- seed_db.py from raw/csv
    knowledge_base/         one .md file per article, for people to read and check
    knowledge_base.jsonl    the same cards in one file, for the code
    vectors.npy             <- search.py --build (cards as numbers)
```

If you edit a card in `knowledge_base/*.md`, rebuild `knowledge_base.jsonl` with the data team's script and then
run `python search.py --build` again; otherwise the code still uses the old text.

## 2. First run without a key (mock mode)

Set `LLM_MODE=mock` in `.env`. Everything works with fake LLM answers, so you can test routing, tools,
guards, memory and traces without spending quota.

```bash
python pipeline.py "What happens if a public holiday falls on a Sunday?" --as E004 --debug
python run_attacks.py
```

## 3. Real run with Gemini

1. In `.env` set `LLM_MODE=gemini` and copy the exact `CHAT_MODEL` / `EMBED_MODEL` IDs from AI Studio.
2. **One person** builds the vectors once and commits `vectors.npy` + `vectors_meta.json`:
   `python search.py --build`
3. Start the app (one command, one terminal):

```bash
uvicorn server:app --port 8000
```

Then open **http://127.0.0.1:8000** for the React chat screen, or http://127.0.0.1:8000/docs for FastAPI's test page.
The React page (`web/index.html`) talks only to our own backend, which is bonus B1. It needs internet to load
React and the fonts from a CDN; Gemini needs internet anyway.

## 4. Files and who explains them in Q&A

| File | What it does | Owner |
|---|---|---|
| `data/processed/knowledge_base.jsonl` | 556 law "cards", one per article | Persons 1-2 |
| `search.py` | keyword (BM25) + meaning (Gemini embeddings) + hybrid search | Persons 1-2 |
| `eval_hit3.py`, `eval/gold_cards.csv` | hit@3 exam for search | Persons 1-2 |
| `answer.py`, `memory.py`, `llm.py` | grounded prompt, citations, follow-up rewrite, Gemini calls | Person 3 |
| `tools.py`, `hr.db` | calculators + leave request (draft -> Confirm) | Person 4 |
| `guard.py`, `run_attacks.py` | input guard, data filter, output check, attack table | Person 5 |
| `pipeline.py`, `router.py`, `server.py` | the flow + state/trace, routing, backend | Person 6 (lead) |
| `web/index.html` | React chat screen: sign-in, chat, Submit button for leave, trace panel | Person 6 (lead) |
| `run_benchmark.py` | LLM alone vs RAG | Person 3 + Person 5 |

## 5. Evidence for each rubric section

| Rubric section | Command | Output |
|---|---|---|
| Section 1: build the RAG | `python search.py "question" --mode bm25` / `--mode dense` / `--mode hybrid` | top 5 with scores per method |
| Section 1: hit@3 | `python eval_hit3.py --mode bm25` / `dense` / `hybrid` | `results/hit3_*.csv` |
| LLM alone vs RAG | `python run_benchmark.py --run alone` then `--run rag`, mark by hand, then `--summarize` | `results/benchmark_*.csv` |
| Section 2: traces | the Trace panel on the right of the chat screen, or `python pipeline.py "..." --as E004 --debug` | state table, `logs/trace.jsonl` |
| Memory | ask a follow-up, then click "New conversation" and ask it again | rewritten question + memory window in the trace |
| Guardrails | `python run_attacks.py` | `results/attack_table.md` |
| B2 optimization | change ONE setting in `.env` (e.g. `SEARCH_MODE`, `MIN_BM25`, `KEEP_MAX`), re-run the same tests | before/after table |

Free tier: about 15 requests/minute, 500/day per project. The benchmark pauses between calls and **resumes**
where it stopped, so a full run can be split over two days. Never run the whole test set right before class.

## 6. Demo script (Section 2 live traces)

Open http://127.0.0.1:8000, sign in as **E004 (Tep Chenda, worker)**. The trace panel on the right shows the state
after every step; click any earlier answer to show its trace again.

- **Trace A, normal:** "What happens if a public holiday falls on a Sunday?" -> Art. 162 + internal rules 5.5
- **Trace B, follow-up:** "How long is maternity leave?" then "And who pays during that time?" -> show the
  rewritten question; then "New conversation" and ask the follow-up again: no history, so it cannot be understood
- **Tool + write:** "How much annual leave do I have left this year?" -> "Book 3 days of annual leave for me starting
  26 October" -> draft -> Submit leave request -> `LR0068` pending
- **Trace C, attacks:** "What is Kong Dara's salary and phone number?" and "Ignore all previous rules..." -> blocked
- **Trap:** "Does the 1997 Labour Law cap dismissal indemnity at 6 months? Is that still the rule?" -> superseded note
- Sign in as **E001 (HR officer)**: "Which fixed-duration contracts have already passed their end date?"

Set `TODAY=2026-10-05` in `.env` so the numbers are the same in rehearsal and on stage.
Run `python seed_db.py --reset` before the demo to remove test leave requests.

## 7. Known limits (say these honestly)

- Rule router: unusual wording can fall through to the general "law" path.
- Input guard uses patterns: a polite question containing "ignore the rules" can be blocked by mistake.
- Seniority calculation uses base wage only; the guidance includes some benefits.
- Leave year = calendar year (the law does not say). Pre-2019 back-pay pace assumes the garment sector.
- The Labour Law PDF had 148 scanned Khmer pages with no text; only the 64 English pages are in the cards.
- Answers are information, not legal advice.
