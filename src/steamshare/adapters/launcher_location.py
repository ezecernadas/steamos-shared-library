"""Where the `steamshare` launcher is: the bash script at the repository root.

The watch service runs steamshare through it, by absolute path, so the
service needs no virtual environment (see the launcher's own header). That
path is fixed when `install` runs: moving the repository afterwards breaks
the service until install runs again.
"""

from pathlib import Path

LAUNCHER_FILE_NAME = "steamshare"

# This file is <repository>/src/steamshare/adapters/launcher_location.py, so
# the repository root is three folders above the one it is in. Resolved, so a
# symlinked checkout still yields the real path systemd will run.
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER_PATH = REPOSITORY_ROOT / LAUNCHER_FILE_NAME
