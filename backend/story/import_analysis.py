"""Local CLI for validating and importing Codex story-map candidates."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from backend.config import PROJECT_ROOT, load_config
from backend.provenance import dlc_provenance

from .analysis_store import AnalysisStore, AnalysisStoreError
from .offline_analysis import OfflineAnalysisError, OfflineAnalysisImporter
from .source import StorySourceLoader


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.story.import_analysis",
        description="Validate or import a local Codex story analysis candidate.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "import"):
        command = actions.add_parser(action)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--map", required=True, dest="candidate", type=Path)
        command.add_argument(
            "--encoding", default="auto", choices=("auto", "utf-8", "gb18030")
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the validate/import command without accepting a caller DLC identity."""

    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        cfg = load_config()
        story_cfg = cfg["story"]
        max_bytes = int(float(story_cfg["max_source_mb"]) * 1024 * 1024)
        analysis_directory = Path(str(story_cfg["analysis_dir"]))
        candidate_directory = Path(str(story_cfg["candidate_dir"]))
        if not analysis_directory.is_absolute():
            analysis_directory = PROJECT_ROOT / analysis_directory
        if not candidate_directory.is_absolute():
            candidate_directory = PROJECT_ROOT / candidate_directory
        importer = OfflineAnalysisImporter(
            StorySourceLoader(max_bytes=max_bytes),
            AnalysisStore(analysis_directory),
            candidate_directory=candidate_directory,
        )
        dlc_version = dlc_provenance(cfg, project_root=PROJECT_ROOT)
        if arguments.action == "validate":
            result = importer.validate(
                arguments.source,
                arguments.candidate,
                encoding=arguments.encoding,
                dlc_version=dlc_version,
            )
            status = "validated"
        else:
            result = importer.import_candidate(
                arguments.source,
                arguments.candidate,
                encoding=arguments.encoding,
                dlc_version=dlc_version,
            )
            status = "imported"
    except (AnalysisStoreError, KeyError, OfflineAnalysisError, OSError, OverflowError, RecursionError, TypeError, UnicodeError, ValueError):
        print("offline analysis failed", file=sys.stderr)
        return 1

    print(
        " ".join(
            (
                f"source_hash={result.key.source_hash[:12]}",
                f"producer={result.key.model}",
                f"analysis_version={result.key.prompt_version}",
                f"dlc_version={result.key.dlc_version}",
                f"chapters={result.chapter_count}",
                f"scenes={result.scene_count}",
                f"status={status}",
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
