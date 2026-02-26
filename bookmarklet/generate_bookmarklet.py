#!/usr/bin/env python3
"""Generate a bookmarklet URL with your Gemini API key embedded."""
import sys
import urllib.parse
from pathlib import Path

def main():
    key = input("Enter your Gemini API key: ").strip()
    if not key:
        print("No key provided. Exiting.")
        return

    js_path = Path(__file__).parent / "studyflow.js"
    js_code = js_path.read_text()

    # Embed the API key
    js_code = js_code.replace('%%GEMINI_KEY%%', key)

    # Minify: remove comments, collapse whitespace
    lines = []
    for line in js_code.split('\n'):
        stripped = line.strip()
        if stripped.startswith('//'):
            continue
        lines.append(stripped)
    minified = ' '.join(lines)

    # Create bookmarklet URL
    bookmarklet = 'javascript:' + urllib.parse.quote(minified, safe="(){}[]!*'~;:@&=+$,/?#")

    print("\n" + "=" * 60)
    print("BOOKMARKLET CREATED!")
    print("=" * 60)
    print("\nSteps:")
    print("1. On your Chromebook, right-click the bookmarks bar")
    print("2. Click 'Add page'")
    print("3. Name it: StudyFlow")
    print("4. In the URL field, paste the ENTIRE text below:")
    print("\n--- COPY EVERYTHING BELOW THIS LINE ---\n")
    print(bookmarklet)
    print("\n--- COPY EVERYTHING ABOVE THIS LINE ---\n")
    print(f"Length: {len(bookmarklet)} chars")

    # Also save to file
    out_path = Path(__file__).parent / "bookmarklet_url.txt"
    out_path.write_text(bookmarklet)
    print(f"Also saved to: {out_path}")

if __name__ == "__main__":
    main()
