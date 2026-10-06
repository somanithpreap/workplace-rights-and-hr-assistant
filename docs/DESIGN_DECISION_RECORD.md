# Design Decision Record (draft - review and put in your own words)

| Field | Our answer |
|---|---|
| Problem and users | Garment workers and HR officers at Mekong Apparel ask the same questions every day about leave, wages, overtime, seniority indemnity and social security. Workers see only their own record; HR officers can see all records. |
| Question types (test list, 48 cases) | 21 answered in one place, 3 need two places, 3 follow-ups, 3 not in the documents, 9 attacks, 9 tool / multi-step (leave balance, indemnity, overtime, holidays, minimum wage, book leave, HR report). |
| Options considered | L1 fixed chain: cannot calculate a worker's own leave or book leave. L2 rule router: question types have clear words ("my leave", "book", "public holidays"). L3 model router: handles odd wording but costs an extra LLM call per question on a 500/day free tier. L4 tool calling: LLM chooses tools and fills inputs - more flexible, but the LLM could choose or fill in an employee ID. L5 agent loop: our questions need at most two tools; a loop adds cost and risk. |
| Decision | **Level 2 rule router + calculator tools chosen by code.** RAG for law questions; tools for "my record"; draft + Confirm for booking leave; referral for disputes. |
| Why this fits | Rules cost 0 tokens and about 1 ms, are easy to test, and keep identity safe: tools get `employee_id` from the login session, never from the model. Calculations are in Python, so numbers are exact and show their formula. |
| Trade-offs | Speed and cost: good (1 LLM call per normal question, 2 for follow-ups). Accuracy: rules miss unusual wording. Safety: strong, because the LLM cannot write to the database or choose whose data to read. |
| When we would change it | If the router sends more than about 10% of test questions down the wrong path, or users write a lot of Khmer / romanised Khmer: move to a model router or a decision model (bonus B3). |

## Other decisions (each one is a likely Q&A question)

| Decision | Choice | Reason |
|---|---|---|
| Chunk unit | One card per legal article (internal rules: one per section) | Articles are the citation unit; 556 cards, about 120 tokens average, largest about 700 tokens, so none exceeds the embedding input limit |
| Search | Hybrid = BM25 + Gemini embeddings, Reciprocal Rank Fusion | Keywords catch "Article 89", "Prakas 442", "NSSF"; meaning search catches paraphrases |
| Relevance filter | keep a card if BM25 >= 3.0 or cosine >= 0.55; max 3 cards | Fewer weak cards for the LLM to "lean on"; tune with the hit@3 and benchmark results |
| Superseded law | Art. 89 and Art. 104-109 carry a SUPERSEDED / ABROGATED note | The 1997 text is otherwise quoted confidently but wrongly |
| Leave year | Calendar year | The law does not say; the tool states this assumption in every answer |
| Daily wage | monthly / 26 | The MLVT guidance's own convention |
| Memory | SQLite per thread + employee; window 6 messages; rewrite only follow-ups | Saves an LLM call on normal questions; thread isolation |
| Writes | Draft first, insert as `pending` only after a Confirm click; managers approve | Guideline tool-safety rule; the bot never approves leave |
