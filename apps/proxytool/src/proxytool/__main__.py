"""`python -m proxytool`: delegate to the ``proxytool`` CLI.

Previously this started a dev server unconditionally (ignoring args), which
made `python -m proxytool --help` launch uvicorn. Now it forwards to the same
argparse CLI the console script uses; the dev-reload workflow is
`uv run uvicorn proxytool.main:app --reload` (as documented in the README).
"""

import sys

from proxytool.cli import main

if __name__ == "__main__":
    sys.exit(main())
