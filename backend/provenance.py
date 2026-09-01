"""Stable content identity for the runtime code and tracked configuration schema."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping


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


def dlc_provenance(
    cfg: Mapping[str, object], *, project_root: Path, waveform_policy: str | None = None
) -> str:
    """Return the stable DLC identity used by both import and runtime lookup.

    The JSON shape and serialization deliberately match the former AppState
    helper byte-for-byte so existing cache identities remain addressable.
    """

    character_value = cfg.get("character")
    character = character_value if isinstance(character_value, Mapping) else {}
    prompt_file = str(character.get("prompt_file") or "")
    prompt_path = Path(prompt_file)
    if prompt_file and not prompt_path.is_absolute():
        prompt_path = project_root / prompt_path
    try:
        prompt_file_sha256 = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        prompt_file_sha256 = ""
    timeline_value = cfg.get("timeline")
    timeline = timeline_value if isinstance(timeline_value, Mapping) else {}
    identity = {
        "role": str(character.get("role") or "default"),
        "profile": str(character.get("profile") or "default"),
        "dlc": str(character.get("dlc_version") or character.get("name") or "default"),
        "prompt": str(character.get("prompt") or ""),
        "examples": list(character.get("examples") or [])[:8],
        "prompt_file_sha256": prompt_file_sha256,
        "waveform_policy": str(
            waveform_policy
            if waveform_policy is not None
            else timeline.get("waveform_policy") or ""
        ),
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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
