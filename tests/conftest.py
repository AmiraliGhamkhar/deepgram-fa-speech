import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure tests never accidentally pick up a real API key from the developer's
# environment or a local .env file.
os.environ.pop("DEEPGRAM_API_KEY", None)
