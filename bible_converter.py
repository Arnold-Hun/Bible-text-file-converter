#!/usr/bin/env python3
"""
MyBible (.SQLite3) to KaiOS (GoBible-optimized JSON) converter.
Part of the KaiOS GoBible project.

This script converts a standard MyBible SQLite3 database into highly optimized JSON files
designed to be stored on an SD Card and read by the KaiOS GoBible application.
It produces:
  - info.json: Translation metadata (abbreviation, full name, language) plus the list of books,
    indices, and chapter counts (small, loaded into memory for navigation).
    Format (info.json, "format": 2):
        {
          "format": 2,
          "abbr": "BSB",
          "name": "Berean Standard Bible",
          "lang": "en",
          "books": [ {"id": 470, "short": "Mt", "full": "Matthew", "chapters": 28}, ... ]
        }
    (The app still accepts the older plain-array info.json files as well.)
  - [book_id].json: Individual files containing scripture text split by chapter and verse (loaded dynamically).

Usage:
  python bible_converter.py <path_to_mybible_db> [translation_name] [--abbr ABBR] [--name "Full Name"]
"""

import os
import sys
import json
import sqlite3
import argparse
import re

# Folder names that say nothing about the translation itself; when the output folder
# has one of these names the abbreviation is taken from the database filename instead.
GENERIC_FOLDER_NAMES = {"default", "bible", "bibles", "importedtranslation"}


def clean_text_field(value, max_len):
    """Strip markup/whitespace from a metadata value; return '' if empty or too long."""
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", "", str(value))
    text = " ".join(text.split())
    return text if 0 < len(text) <= max_len else ""


def read_module_info(cursor, tables):
    """Reads the optional MyBible 'info' table (name/value rows) into a lowercase dict."""
    info = {}
    if "info" not in tables:
        return info
    try:
        cursor.execute("SELECT name, value FROM info;")
        for key, value in cursor.fetchall():
            info[str(key).strip().lower()] = value
    except Exception as e:
        print(f"[*] Note: could not read 'info' table ({e}); continuing without it.")
    return info


def derive_abbr(translation_name, db_path):
    """Fallback abbreviation: first word of the translation name, or the DB filename."""
    words = translation_name.split()
    first = words[0] if words else ""
    if first and first.lower() not in GENERIC_FOLDER_NAMES:
        return first
    stem = os.path.splitext(os.path.basename(db_path))[0]
    stem = "".join(c for c in stem if c.isalnum() or c in ("-", "_"))
    return stem or first or "BIBLE"

def convert_bible(db_path, translation_name=None, min_book=None, max_book=None, out_root="Bibles",
                  abbr=None, full_name=None, lang=None):
    if not os.path.exists(db_path):
        print(f"Error: Database file not found at '{db_path}'")
        sys.exit(1)

    # Inferred translation name from database filename if not specified
    if not translation_name:
        translation_name = os.path.splitext(os.path.basename(db_path))[0]
        # Clean translation name
        translation_name = "".join(c for c in translation_name if c.isalnum() or c in ('-', '_')).strip()
        if not translation_name:
            translation_name = "ImportedTranslation"

    print(f"[*] Starting conversion of '{db_path}'...")
    print(f"[*] Translation folder name: {translation_name}")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

    # Verify tables
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        print(f"[*] Found tables: {', '.join(tables)}")
        
        if 'books' not in tables or 'verses' not in tables:
            print("Error: The database does not contain standard MyBible 'books' or 'verses' tables.")
            sys.exit(1)
    except Exception as e:
        print(f"Error reading database metadata: {e}")
        sys.exit(1)

    # Translation metadata (abbreviation shown in the app header, full name, language)
    module_info = read_module_info(cursor, tables)

    abbr = " ".join((abbr or "").split())
    abbr_source = "--abbr"
    if not abbr:
        abbr = derive_abbr(translation_name, db_path)
        abbr_source = "derived (use --abbr to set it explicitly)"

    full_name = " ".join((full_name or "").split())
    if not full_name:
        full_name = clean_text_field(module_info.get("description"), 60) or translation_name

    lang = " ".join((lang or "").split())
    if not lang:
        lang = clean_text_field(module_info.get("language"), 12)

    print(f"[*] Abbreviation: {abbr}  ({abbr_source})")
    print(f"[*] Full name:    {full_name}")
    if lang:
        print(f"[*] Language:     {lang}")
    if len(abbr) > 8:
        print(f"[Warning] Abbreviation '{abbr}' is longer than 8 characters; "
              f"the app header will show it cut with an ellipsis.")

    # 1. Fetch books
    print("[*] Reading books metadata...")
    try:
        # MyBible books table has book_number, short_name, long_name
        # Some tables might have slightly different names, we query columns first to be safe
        cursor.execute("PRAGMA table_info(books);")
        books_cols = {col[1]: col[2] for col in cursor.fetchall()}
        
        # Determine exact columns
        col_id = 'book_number' if 'book_number' in books_cols else 'id'
        col_short = 'short_name' if 'short_name' in books_cols else ('short' if 'short' in books_cols else 'short_name')
        col_full = 'long_name' if 'long_name' in books_cols else ('name' if 'name' in books_cols else 'long_name')

        print(f"[*] Using books columns: ID={col_id}, Short={col_short}, Full={col_full}")
        cursor.execute(f"SELECT {col_id}, {col_short}, {col_full} FROM books ORDER BY {col_id};")
        raw_books = cursor.fetchall()
    except Exception as e:
        print(f"Error querying 'books' table: {e}")
        sys.exit(1)

    # Optional book-range filter (e.g. --min-book 470 to export New Testament only,
    # using MyBible's book_number convention where NT starts at 470)
    if min_book is not None or max_book is not None:
        before = len(raw_books)
        raw_books = [
            b for b in raw_books
            if (min_book is None or int(b[0]) >= min_book)
            and (max_book is None or int(b[0]) <= max_book)
        ]
        print(f"[*] Book filter applied ({min_book}-{max_book}): {before} -> {len(raw_books)} books")

    # Prepare output directory
    output_dir = os.path.join(out_root, translation_name)
    os.makedirs(output_dir, exist_ok=True)
    print(f"[*] Output directory created at: {output_dir}")

    info_data = []
    converted_count = 0

    # 2. Iterate books and process verses
    for book_id, short_name, long_name in raw_books:
        # Make sure book_id is integer
        book_id = int(book_id)
        print(f"    Processing: [{book_id:02d}] {short_name} - {long_name}...")

        # Fetch verses for this book
        # MyBible verses table has book_number, chapter, verse, text
        try:
            cursor.execute("PRAGMA table_info(verses);")
            verses_cols = {col[1]: col[2] for col in cursor.fetchall()}
            
            v_col_book = 'book_number' if 'book_number' in verses_cols else 'book'
            v_col_chapter = 'chapter'
            v_col_verse = 'verse'
            v_col_text = 'text'

            cursor.execute(
                f"SELECT {v_col_chapter}, {v_col_verse}, {v_col_text} "
                f"FROM verses WHERE {v_col_book} = ? "
                f"ORDER BY {v_col_chapter}, {v_col_verse};",
                (book_id,)
            )
            raw_verses = cursor.fetchall()
        except Exception as e:
            print(f"Error querying verses for book {book_id}: {e}")
            continue

        if not raw_verses:
            print(f"    [Warning] No verses found for book {book_id}. Skipping.")
            continue

        # Structure: chapter -> verse -> text
        book_content = {}
        chapters_set = set()

        for chapter, verse, text in raw_verses:
            # MyBible chapter/verse are numbers, text is string
            ch_str = str(chapter)
            v_str = str(verse)
            chapters_set.add(int(chapter))

            if ch_str not in book_content:
                book_content[ch_str] = {}
            
            # Clean text (remove XML/HTML tags often found in MyBible databases, like <b>, <i>, <pb/>, etc.)
            clean_text = text if text else ""
            # Simple tag removal
            clean_text = re.sub(r'<[^>]+>', '', clean_text)
            # Remove redundant whitespaces
            clean_text = " ".join(clean_text.split())

            book_content[ch_str][v_str] = clean_text

        # Get max chapters
        total_chapters = max(chapters_set) if chapters_set else 0

        # Append to info.json list
        info_data.append({
            "id": book_id,
            "short": short_name,
            "full": long_name,
            "chapters": total_chapters
        })

        # Save [book_id].json
        book_json_path = os.path.join(output_dir, f"{book_id}.json")
        try:
            with open(book_json_path, 'w', encoding='utf-8') as f:
                json.dump(book_content, f, ensure_ascii=False, indent=2)
            converted_count += 1
        except Exception as e:
            print(f"Error saving {book_json_path}: {e}")

    # 3. Save info.json (translation metadata + book index)
    info_doc = {"format": 2, "abbr": abbr, "name": full_name}
    if lang:
        info_doc["lang"] = lang
    info_doc["books"] = info_data

    info_json_path = os.path.join(output_dir, "info.json")
    try:
        with open(info_json_path, 'w', encoding='utf-8') as f:
            json.dump(info_doc, f, ensure_ascii=False, indent=2)
        print(f"[+] Successfully wrote index to {info_json_path}")
    except Exception as e:
        print(f"Error saving info.json: {e}")
        sys.exit(1)

    print("\n=======================================================")
    print(f"[SUCCESS] Conversion completed!")
    print(f"  - Translation: {abbr} - {full_name}")
    print(f"  - Total books converted: {converted_count}")
    print(f"  - Files outputted in: {os.path.abspath(output_dir)}")
    print("=======================================================")
    print("To install on KaiOS SD Card:")
    print(f"  Copy the entire 'Bibles' folder to the root of your SD Card.")
    print("  So it exists as: /sdcard/Bibles/ or /storage/sdcard/Bibles/")
    print("=======================================================")

    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="KaiOS GoBible Desktop SQLite Converter Utility",
        epilog="Example (full Bible, for SD card):\n"
               "  python bible_converter.py NIV.SQLite3 NIV --abbr NIV --name \"New International Version\"\n"
               "Example (New Testament only, bundled Default Bible = BSB):\n"
               "  python bible_converter.py BSB.SQLite3 default --abbr BSB "
               "--name \"Berean Standard Bible\" --min-book 470 --out bible\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("db_path", help="Path to the MyBible .SQLite3 file")
    parser.add_argument("translation_name", nargs="?", default=None,
                         help="Output folder name (defaults to the DB filename)")
    parser.add_argument("--min-book", type=int, default=None,
                         help="Only include books with book_number >= this value "
                              "(e.g. 470 = New Testament start in MyBible's numbering)")
    parser.add_argument("--max-book", type=int, default=None,
                         help="Only include books with book_number <= this value")
    parser.add_argument("--out", default="Bibles",
                         help="Output root folder. Use 'Bibles' for SD-card translations "
                              "(default), or e.g. 'bible' to bundle a translation inside "
                              "the app package for offline fetch() loading.")
    parser.add_argument("--abbr", default=None,
                         help="Short translation abbreviation shown in the app header, e.g. NIV "
                              "(keep it at 8 characters or fewer). Defaults to the first word of "
                              "the translation name / the DB filename.")
    parser.add_argument("--name", default=None,
                         help="Full translation name, e.g. \"Berean Standard Bible\" "
                              "(defaults to the MyBible 'description' info, else the translation name).")
    parser.add_argument("--lang", default=None,
                         help="Language code, e.g. en or hu (defaults to the MyBible 'language' info, if any).")
    args = parser.parse_args()

    convert_bible(args.db_path, args.translation_name,
                  min_book=args.min_book, max_book=args.max_book, out_root=args.out,
                  abbr=args.abbr, full_name=args.name, lang=args.lang)
