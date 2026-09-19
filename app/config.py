"""运行期配置。全部可用环境变量覆盖，方便在容器 / K8s 里改。"""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_URL = os.getenv(
    "RADAR_SOURCE_URL",
    "https://raw.githubusercontent.com/ripienaar/free-for-dev/master/README.md",
)
DB_PATH = Path(os.getenv("RADAR_DB_PATH", "./data/radar.db"))
USER_AGENT = os.getenv("RADAR_USER_AGENT", "free-for-dev-radar/0.1 (+https://github.com/)")
TIMEOUT = float(os.getenv("RADAR_TIMEOUT", "30"))
