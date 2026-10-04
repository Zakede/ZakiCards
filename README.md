# ザキCards (ZakiCards)

Flashcards on a black screen. Made for OLED laptops: pure black, dim text, nothing stays in one spot long enough to burn in.

Logo: the Z from my capybara logo set (Capylogo.ai).

## What it does
- Loops each card: question → answer → black (default 5s / 5s / 10s, presets + live tweaks).
- Deck sources:
  - **Anki**: reads your Anki profiles directly (from a copy, never writes to Anki). Includes subdecks, filters for studied / due / new cards.
  - **Import**: Anki `.apkg` / `.colpkg` packages (old and new formats, images included) and CSV / TSV / TXT word lists.
  - **My decks**: type your own in the app (`front | back`, one per line).
- Burn-in care: cards land in a slightly different spot each time, brightness control, dim drifting clock in the black gap (optional), no permanent UI.
- Top-edge drop-down bar: prev / pause / skip, timing steppers, brightness, fullscreen, menu, quit.
- Keeps the screen awake while playing (optional).
- UI in English or 日本語.

Keys: Space/→ skip · ← previous · Enter answer · P pause · B black · ↑/↓ brightness · F fullscreen · Esc menu · Q quit

## Tech
Python + pywebview (Edge WebView2), a small local HTTP server, one HTML/CSS/JS file for the UI.
`anki_reader.py` is a from-scratch reader for Anki's SQLite format (protobuf configs, card templates,
cloze, furigana, conditionals), so no Anki code is bundled.

## Run / build
    pip install pywebview zstandard
    pythonw zakicards.pyw      # run from source
    build.bat                  # -> dist\ZakiCards\ZakiCards.exe + release\ZakiCards-win64.zip

Settings and your own decks live in `%APPDATA%\ZakiCards`.

"Anki" is a trademark of its owners; ZakiCards is an independent app that reads Anki deck files.
