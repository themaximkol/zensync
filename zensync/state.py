"""
Persistent per-device state: device_id and last sync metadata.
Stored at platformdirs.user_data_dir("zensync") / "state.json".
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from platformdirs import user_data_dir

_STATE_DIR = Path(user_data_dir("zensync"))
DEFAULT_STATE_PATH = _STATE_DIR / "state.json"


@dataclass
class State:
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    last_pushed_snapshot_id: Optional[str] = None
    last_pulled_snapshot_id: Optional[str] = None
    last_local_hash: Optional[str] = None

    def save(self, path: Path = DEFAULT_STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> "State":
        """Load state from disk, creating and persisting a fresh one if absent."""
        if not path.is_file():
            s = cls()
            s.save(path)
            return s
        data = json.loads(path.read_text(encoding="utf-8"))
        # Ignore unknown keys so old state files stay forward-compatible
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
