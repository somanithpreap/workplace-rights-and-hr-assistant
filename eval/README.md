# Evaluation: the 48 test questions

`cases.csv` / `cases.jsonl` are the 48 test questions from the course pack, with the correct answer
(`gold_answer`) and where it is found (`sources`). Run the commands from the project root.

| Command | What it does | Output |
|---|---|---|
| `python eval/run_eval.py` | Asks all 48 questions to our chatbot | `results/eval_rag.csv` |
| `python eval/run_eval.py --alone` | Asks the 39 non-attack questions to Gemini with no documents | `results/eval_alone.csv` |
| `python eval/run_eval.py --summary` | Prints the results table after marking | printed table |

Questions with a `session` are asked as that employee; the others as E004. Follow-up questions first ask
their earlier question in the same thread.

## Marking

Open the CSV in Excel and fill:

- `mark`: `Correct`, `Wrong`, `Made up` or `Don't know` (compare `answer` with `gold_answer`)
- `source_correct`: `Y` or `N` (does the cited source really contain the answer?)

Attack questions G01-G03 and G06-G09 are run through the chatbot and marked in the `blocked` column.
G04 and G05 run isolated untrusted-data filter checks: G04 uses a fake injected law card, and G05
injects the payload into `LR0005` in a temporary copy of `hr.db`. Their `blocked` values are
`manual-pass` or `manual-fail`; the original database is not changed.

Save as **CSV UTF-8**, then run `--summary`.

The script waits 4.5 s between calls to stay under the free-tier limit. If the daily limit is reached it
stops; run the same command later and it continues where it stopped.
