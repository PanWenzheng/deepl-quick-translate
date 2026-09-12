"""``python3 -m app`` 入口。"""

from __future__ import annotations

import sys

from .application import main

if __name__ == "__main__":
    sys.exit(main())
