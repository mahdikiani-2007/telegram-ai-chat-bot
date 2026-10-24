"""L-Bot entrypoint — modular layout.

All logic now lives in focused modules (config, db, ai, bots, services/,
handlers/, wiring.py); this file only preserves the historic ``python3
bot.py`` entrypoint used by start.sh and the Dockerfile.
"""
from main import main

if __name__ == "__main__":
    main()
