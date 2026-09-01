"""Stable content identity for the runtime code and tracked configuration schema."""

from __future__ import annotations

import hashlib
from pathlib import Path


RUNTIME_FINGERPRINT_FILES = (
    "backend/__init__.py",
    "backend/audio.py",
    "backend/camera.py",
    "backend/config.py",
    "backend/device_ops.py",
    "backend/game_loop.py",
    "backend/llm.py",
    "backend/logging_utils.py",
    "backend/main.py",
    "backend/output_coordinator.py",
    "backend/provenance.py",
    "backend/relay_client.py",
    "backend/safety.py",
    "backend/timeline/__init__.py",
    "backend/timeline/cycle_runner.py",
    "backend/timeline/models.py",
    "backend/timeline/player.py",
    "backend/timeline/randomizer.py",
    "backend/timeline/replay_store.py",
    "backend/timeline/session.py",
    "backend/waveforms.py",
    "config/character.example.yaml",
    "config/config.example.yaml",
    "config/waveforms.yaml",
)


def runtime_content_fingerprint(project_root: Path) -> str:
    """Hash every readable allowlisted input with stable path boundaries."""
    digest = hashlib.sha256()
    readable = 0
    for relative_path in RUNTIME_FINGERPRINT_FILES:
        try:
            source_bytes = (project_root / relative_path).read_bytes()
        except OSError:
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_bytes)
        digest.update(b"\0")
        readable += 1
    if not readable:
        raise RuntimeError("runtime fingerprint content is unavailable")
    return f"source-sha256:{digest.hexdigest()}"
