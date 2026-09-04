"""Entry point for `python -m ai_crontab`, which is how a supervisor is spawned."""

import sys

from .cli import main

sys.exit(main())
