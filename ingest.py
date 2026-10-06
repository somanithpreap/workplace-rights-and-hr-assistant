"""
Data Ingestion & Cleaning Script
Scans docs/ (flat or subfolders) for .md and .pdf files, and seed/ for CSVs,
outputting a unified knowledge_base.json
"""

import csv
import json
from pathlib import Path
import re
import pypdf

DOCS_DIR = Path("docs")
SEED_DIR = Path("seed")
OUTPUT_FILE = Path("knowledge_base.json")


def clean_text(text: str) -> str:
    """Removes excessive whitespace and standardizes line breaks."""
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str, source_name: str, doc_type: str, chunk_size: int = 600, overlap: int = 100
) -> list[dict]:
    """Splits text into overlapping chunks for retrieval."""
    chunks = []
    text = clean_text(text)
    if not text:
        return chunks

    for i in range(0, len(text), chunk_size - overlap):
        chunk_str = text[i : i + chunk_size]
        if len(chunk_str.strip()) > 50:
            chunks.append(
                {
                    "id": f"{Path(source_name).stem}_c{i//chunk_size + 1}",
                    "type": doc_type,
                    "source": source_name,
                    "title": Path(source_name).stem.replace("-", " ").replace("_", " ").title(),
                    "content": chunk_str,
                }
            )
    return chunks


def process_markdown() -> list[dict]:
    """Parses all .md files anywhere under docs/."""
    chunks = []
    md_files = list(DOCS_DIR.rglob("*.md"))
    for md_path in md_files:
        print(f" Processing Markdown: {md_path.name}")
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()
            chunks.extend(chunk_text(content, md_path.name, "company_rules_md"))
        except Exception as e:
            print(f"Error reading {md_path.name}: {e}")
    return chunks


def process_pdfs() -> list[dict]:
    """Parses all .pdf files anywhere under docs/."""
    chunks = []
    pdf_files = list(DOCS_DIR.rglob("*.pdf"))
    for pdf_path in pdf_files:
        print(f" Processing PDF: {pdf_path.name}")
        try:
            reader = pypdf.PdfReader(pdf_path)
            for page_num, page in enumerate(reader.pages, start=1):
                raw_text = page.extract_text() or ""
                page_chunks = chunk_text(raw_text, pdf_path.name, "legal_pdf")
                for c in page_chunks:
                    c["id"] = f"{c['id']}_p{page_num}"
                    c["title"] += f" (Page {page_num})"
                chunks.extend(page_chunks)
        except Exception as e:
            print(f"Error processing {pdf_path.name}: {e}")
    return chunks


def process_csvs() -> list[dict]:
    """Converts reference CSVs into searchable entries."""
    csv_chunks = []

    min_wage_csv = SEED_DIR / "minimum_wage_garment_sector.csv"
    if min_wage_csv.exists():
        print(f" Processing CSV: {min_wage_csv.name}")
        with open(min_wage_csv, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                content = (
                    f"Garment Sector Minimum Wage Year {row.get('year', '')}: "
                    f"${row.get('monthly_minimum_wage_usd', '')} USD per month. "
                    f"Note: {row.get('note', '')}"
                )
                csv_chunks.append(
                    {
                        "id": f"min_wage_{row.get('year', 'unknown')}",
                        "type": "reference_csv",
                        "source": min_wage_csv.name,
                        "title": f"Minimum Wage Regulation {row.get('year', '')}",
                        "content": content,
                    }
                )

    holidays_csv = SEED_DIR / "public_holidays_2026.csv"
    if holidays_csv.exists():
        print(f" Processing CSV: {holidays_csv.name}")
        with open(holidays_csv, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            holiday_list = [
                f"{row.get('date', '')} ({row.get('weekday', '')}): {row.get('name', '')}"
                for row in reader
            ]
            content = "Official Cambodian Public Holidays 2026:\n" + "\n".join(holiday_list)
            csv_chunks.append(
                {
                    "id": "public_holidays_2026",
                    "type": "reference_csv",
                    "source": holidays_csv.name,
                    "title": "Public Holidays 2026 Schedule",
                    "content": content,
                }
            )

    return csv_chunks


def main():
    print("=== Building Unified Knowledge Base ===")
    if not DOCS_DIR.exists():
        DOCS_DIR.mkdir()

    md_chunks = process_markdown()
    pdf_chunks = process_pdfs()
    csv_chunks = process_csvs()

    all_knowledge = md_chunks + pdf_chunks + csv_chunks

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_knowledge, f, indent=2, ensure_ascii=False)

    print(
        f"\nDone! Saved '{OUTPUT_FILE}' with {len(all_knowledge)} total items "
        f"({len(md_chunks)} MD + {len(pdf_chunks)} PDF + {len(csv_chunks)} CSV)."
    )


if __name__ == "__main__":
    main()