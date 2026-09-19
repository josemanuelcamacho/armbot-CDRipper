#!/usr/bin/env python3
"""
Audio CD ripping and AccurateRip verification wrapper.

Capabilities
------------
1. Opens/ejects the optical-drive tray.
2. Closes the tray.
3. Checks whether an audio CD is readable.
4. Rips the audio CD to FLAC using abcde.
5. Keeps the physical CD in the drive during verification.
6. Verifies the tracks against AccurateRip using ARver.
7. Parses cdparanoia/abcde diagnostics into an error summary.
8. Writes:
       abcde.log
       accuraterip.log
       rip_report.json
9. Optionally ejects the disc after completion.

Important
---------
- This script is intended for Linux.
- abcde handles audio CDs, not arbitrary data CD-ROM filesystems.
- AccurateRip verification requires Internet access.
- The CD must remain in the drive until ARver finishes.
- The drive read-offset correction must be configured correctly for reliable
  AccurateRip comparison.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Data structures used in the final report
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    """Result returned after running an external command."""

    command: list[str]
    return_code: int
    stdout: str
    stderr: str

    @property
    def successful(self) -> bool:
        return self.return_code == 0


@dataclass
class ReadingStatistics:
    """
    Statistics inferred from abcde/cdparanoia output.

    cdparanoia output varies between distributions and versions. These counts
    are therefore diagnostic counters, not guaranteed sector-level totals.
    The unmodified logs remain the authoritative diagnostic record.
    """

    detected_read_events: int = 0
    corrected_or_retried_events: int = 0
    unrecoverable_events: int = 0

    overlap_events: int = 0
    drift_events: int = 0
    dropped_or_skipped_events: int = 0
    transport_errors: int = 0

    matched_lines: list[str] = field(default_factory=list)


@dataclass
class AccurateRipStatistics:
    """Summary inferred from ARver's human-readable output."""

    database_available: bool = False
    total_tracks_found: int = 0
    verified_tracks: int = 0
    failed_tracks: int = 0
    unavailable_tracks: int = 0
    confidence_values: list[int] = field(default_factory=list)
    verification_successful: bool = False


@dataclass
class RipReport:
    """Complete result of a ripping and verification session."""

    started_at: str
    finished_at: str
    device: str
    output_directory: str

    abcde_return_code: int
    accuraterip_return_code: int | None

    rip_successful: bool
    verification_successful: bool

    audio_files: list[str]

    reading_statistics: ReadingStatistics
    accuraterip_statistics: AccurateRipStatistics

    messages: list[str] = field(default_factory=list)


class CDOperationError(RuntimeError):
    """Raised when a required CD operation fails."""


# ---------------------------------------------------------------------------
# General command execution
# ---------------------------------------------------------------------------

def run_command(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = False,
) -> CommandResult:
    """
    Run an external program without invoking a shell.

    Avoiding shell=True makes paths and arguments safer and prevents shell
    expansion or command injection.
    """
    try:
        process = subprocess.run(
            list(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CDOperationError(
            f"Required executable was not found: {command[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CDOperationError(
            f"Command timed out after {timeout} seconds: {' '.join(command)}"
        ) from exc

    result = CommandResult(
        command=list(command),
        return_code=process.returncode,
        stdout=process.stdout or "",
        stderr=process.stderr or "",
    )

    if check and not result.successful:
        detail = result.stderr.strip() or result.stdout.strip() or "No details."
        raise CDOperationError(
            f"Command failed with status {result.return_code}: "
            f"{' '.join(result.command)}\n{detail}"
        )

    return result


def require_programs(programs: Sequence[str]) -> None:
    """Fail early when a required command is not installed."""

    missing = [program for program in programs if shutil.which(program) is None]

    if missing:
        raise CDOperationError(
            "Missing required programs: "
            + ", ".join(missing)
            + ". Install them before running this script."
        )


# ---------------------------------------------------------------------------
# Optical-drive tray operations
# ---------------------------------------------------------------------------

def eject_drive(device: str = "/dev/cdrom") -> CommandResult:
    """
    Open/eject the optical-drive tray.

    Equivalent shell command:
        eject /dev/cdrom
    """
    return run_command(["eject", device], check=True)


def close_drive(device: str = "/dev/cdrom") -> CommandResult:
    """
    Close the optical-drive tray.

    The '-t' option requests tray closure. Some slot-loading or unusual drives
    may not support software-controlled closing.
    """
    return run_command(["eject", "-t", device], check=True)


def wait_for_audio_cd(
    device: str = "/dev/cdrom",
    *,
    timeout: float = 45.0,
    polling_interval: float = 1.0,
) -> CommandResult:
    """
    Wait until cdparanoia can read an audio-CD table of contents.

    'cdparanoia -Q' queries the disc without ripping it.
    """
    deadline = time.monotonic() + timeout
    last_result: CommandResult | None = None

    while time.monotonic() < deadline:
        last_result = run_command(
            ["cdparanoia", "-d", device, "-Q"],
            timeout=15,
        )

        combined = f"{last_result.stdout}\n{last_result.stderr}".lower()

        # A successful TOC query normally identifies audio tracks.
        if last_result.successful and (
            "track" in combined or "audio" in combined
        ):
            return last_result

        time.sleep(polling_interval)

    details = ""
    if last_result:
        details = last_result.stderr.strip() or last_result.stdout.strip()

    raise CDOperationError(
        f"No readable audio CD was detected in {device} within "
        f"{timeout:.0f} seconds. {details}"
    )


# ---------------------------------------------------------------------------
# abcde configuration and ripping
# ---------------------------------------------------------------------------

def shell_single_quote(value: str) -> str:
    """
    Quote a value for abcde's shell-style configuration syntax.

    abcde reads configuration as shell code. This function safely represents
    an arbitrary path as one single-quoted shell value.
    """
    return "'" + value.replace("'", "'\"'\"'") + "'"


def create_abcde_config(
    config_path: Path,
    output_directory: Path,
    temporary_directory: Path,
    device: str,
    drive_offset: int | None,
) -> None:
    """
    Create a temporary abcde configuration.

    EJECTCD is deliberately disabled because ARver needs the physical CD still
    present to calculate the AccurateRip disc ID from its table of contents.

    CDPARANOIAOPTS may include '-O <offset>' when a known drive read offset is
    supplied. AccurateRip comparison is unreliable when this correction is
    wrong or absent.
    """
    cdparanoia_options = "-v"

    if drive_offset is not None:
        cdparanoia_options += f" -O {drive_offset}"

    config_text = f"""\
# Automatically generated by cd_rip_verify.py

CDROM={shell_single_quote(device)}

OUTPUTTYPE=flac
OUTPUTDIR={shell_single_quote(str(output_directory))}
WAVOUTPUTDIR={shell_single_quote(str(temporary_directory))}

# Stable numeric track prefixes make verification ordering easier.
PADTRACKS=y
OUTPUTFORMAT='${{ARTISTFILE}}-${{ALBUMFILE}}/${{TRACKNUM}}-${{TRACKFILE}}'
VAOUTPUTFORMAT='Various-${{ALBUMFILE}}/${{TRACKNUM}}-${{ARTISTFILE}}-${{TRACKFILE}}'

# Keep the disc loaded until AccurateRip verification is complete.
EJECTCD=n

# Run without metadata-selection prompts.
INTERACTIVE=n

# cdparanoia diagnostics and optional sample-offset correction.
CDPARANOIAOPTS={shell_single_quote(cdparanoia_options)}

# Preserve detailed abcde diagnostics.
EXTRAVERBOSE=2
"""

    config_path.write_text(config_text, encoding="utf-8")


def rip_with_abcde(
    device: str,
    output_directory: Path,
    temporary_directory: Path,
    config_path: Path,
) -> CommandResult:
    """
    Rip all audio tracks to FLAC using abcde.

    Actions:
      cddb   - obtain metadata
      read   - extract audio
      encode - encode WAV audio to FLAC
      tag    - write metadata
      move   - move completed files to OUTPUTDIR
      clean  - remove normal temporary files

    '-N' enables abcde's noninteractive mode.
    '-p' pads track numbers.
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    temporary_directory.mkdir(parents=True, exist_ok=True)

    command = [
        "abcde",
        "-N",
        "-p",
        "-d",
        device,
        "-c",
        str(config_path),
        "-o",
        "flac",
        "-a",
        "cddb,read,encode,tag,move,clean",
    ]

    return run_command(command)


# ---------------------------------------------------------------------------
# Locate and order the output tracks
# ---------------------------------------------------------------------------

TRACK_NUMBER_PATTERN = re.compile(
    r"(?<!\d)(?P<track>\d{1,3})(?=[\s._-])"
)


def track_sort_key(path: Path) -> tuple[int, str]:
    """
    Sort audio files using the first track-like number in their filename.

    The abcde configuration produces names beginning with a padded track
    number, so this normally preserves CD track order.
    """
    match = TRACK_NUMBER_PATTERN.search(path.name)

    if match:
        return int(match.group("track")), path.name.casefold()

    return 9999, path.name.casefold()


def find_ripped_tracks(output_directory: Path) -> list[Path]:
    """Find all FLAC tracks generated beneath the requested output directory."""

    files = [
        file
        for file in output_directory.rglob("*.flac")
        if file.is_file()
    ]

    return sorted(files, key=track_sort_key)


# ---------------------------------------------------------------------------
# AccurateRip verification
# ---------------------------------------------------------------------------

def verify_with_arver(
    device: str,
    tracks: Sequence[Path],
) -> CommandResult:
    """
    Verify ripped files with ARver and the AccurateRip database.

    ARver's CLI can vary slightly by release. The normal invocation accepts
    the device and an ordered list of track files. If the installed version
    rejects '-d', run 'arver --help' and change ARVER_DEVICE_ARGUMENT below.
    """
    if not tracks:
        raise CDOperationError(
            "No FLAC tracks were found, so AccurateRip verification "
            "cannot be performed."
        )

    # Current/common ARver invocation.
    command = [
        "arver",
        "-d",
        device,
        *[str(track) for track in tracks],
    ]

    return run_command(command)


# ---------------------------------------------------------------------------
# Diagnostic parsing
# ---------------------------------------------------------------------------

def count_matching_lines(
    lines: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
) -> tuple[int, list[str]]:
    """
    Count log lines matching any supplied regular expression.

    Each line is counted once even if several patterns match.
    """
    matched: list[str] = []

    for line in lines:
        cleaned = line.strip()
        if cleaned and any(pattern.search(cleaned) for pattern in patterns):
            matched.append(cleaned)

    return len(matched), matched


def parse_cdparanoia_statistics(log_text: str) -> ReadingStatistics:
    """
    Extract conservative diagnostics from abcde/cdparanoia output.

    cdparanoia commonly reports conditions such as:
      overlap
      drift
      scratch
      dropped bytes
      skipped sectors
      transport errors
      retries/re-reads
      unrecoverable errors

    Because message wording differs across versions, this function reports
    event lines rather than claiming exact damaged-sector counts.
    """
    lines = log_text.splitlines()

    detected_patterns = [
        re.compile(
            r"\b(error|scratch|skip(?:ped)?|drop(?:ped)?|"
            r"drift|overlap|retry|re-?read|transport)\b",
            re.IGNORECASE,
        )
    ]

    corrected_patterns = [
        re.compile(
            r"\b(corrected|recovered|verified|retry|retried|"
            r"re-?read|overlap)\b",
            re.IGNORECASE,
        )
    ]

    unrecoverable_patterns = [
        re.compile(
            r"\b(unrecoverable|uncorrectable|failed|fatal|"
            r"giving up|read error)\b",
            re.IGNORECASE,
        )
    ]

    overlap_patterns = [
        re.compile(r"\boverlap\b", re.IGNORECASE),
    ]

    drift_patterns = [
        re.compile(r"\bdrift\b", re.IGNORECASE),
    ]

    dropped_patterns = [
        re.compile(
            r"\b(dropped|skipped|missing sector|hole)\b",
            re.IGNORECASE,
        ),
    ]

    transport_patterns = [
        re.compile(
            r"\b(transport error|scsi error|i/o error|input/output error)\b",
            re.IGNORECASE,
        ),
    ]

    detected_count, detected_lines = count_matching_lines(
        lines, detected_patterns
    )
    corrected_count, corrected_lines = count_matching_lines(
        lines, corrected_patterns
    )
    unrecoverable_count, unrecoverable_lines = count_matching_lines(
        lines, unrecoverable_patterns
    )
    overlap_count, overlap_lines = count_matching_lines(
        lines, overlap_patterns
    )
    drift_count, drift_lines = count_matching_lines(
        lines, drift_patterns
    )
    dropped_count, dropped_lines = count_matching_lines(
        lines, dropped_patterns
    )
    transport_count, transport_lines = count_matching_lines(
        lines, transport_patterns
    )

    # Preserve unique diagnostic lines in their original order.
    all_matched = []
    seen = set()

    for line in (
        detected_lines
        + corrected_lines
        + unrecoverable_lines
        + overlap_lines
        + drift_lines
        + dropped_lines
        + transport_lines
    ):
        if line not in seen:
            seen.add(line)
            all_matched.append(line)

    return ReadingStatistics(
        detected_read_events=detected_count,
        corrected_or_retried_events=corrected_count,
        unrecoverable_events=unrecoverable_count,
        overlap_events=overlap_count,
        drift_events=drift_count,
        dropped_or_skipped_events=dropped_count,
        transport_errors=transport_count,
        matched_lines=all_matched,
    )


def parse_accuraterip_statistics(
    output_text: str,
    return_code: int,
    expected_track_count: int,
) -> AccurateRipStatistics:
    """
    Parse common ARver result wording.

    ARver remains the authority: the complete unmodified output is saved in
    accuraterip.log. This parser only creates a compact machine-readable
    summary from common terms such as verified, accurate, failed, N/A and
    confidence.
    """
    lines = [line.strip() for line in output_text.splitlines() if line.strip()]
    lowered = output_text.lower()

    database_available = not any(
        phrase in lowered
        for phrase in (
            "not found in accuraterip",
            "no accuraterip data",
            "database entry not found",
            "failed to fetch",
            "network error",
        )
    )

    verified_patterns = (
        r"\baccurately ripped\b",
        r"\bverified\b",
        r"\bmatch(?:ed|es)?\b.*\bconfidence\b",
        r"\bpass(?:ed)?\b",
    )

    failed_patterns = (
        r"\bverification failed\b",
        r"\bnot accurate\b",
        r"\bdoes not match\b",
        r"\bmismatch\b",
        r"\bfailed\b",
    )

    unavailable_patterns = (
        r"\bn/?a\b",
        r"\bnot present\b",
        r"\bnot found in accuraterip\b",
        r"\bno accuraterip data\b",
        r"\bcannot be verified\b",
    )

    verified_lines = {
        line
        for line in lines
        if any(re.search(pattern, line, re.IGNORECASE)
               for pattern in verified_patterns)
        and not any(re.search(pattern, line, re.IGNORECASE)
                    for pattern in failed_patterns)
    }

    failed_lines = {
        line
        for line in lines
        if any(re.search(pattern, line, re.IGNORECASE)
               for pattern in failed_patterns)
    }

    unavailable_lines = {
        line
        for line in lines
        if any(re.search(pattern, line, re.IGNORECASE)
               for pattern in unavailable_patterns)
    }

    confidence_values = [
        int(value)
        for value in re.findall(
            r"\bconfidence(?:\s*[:=]|\s+of)?\s*(\d+)\b",
            output_text,
            re.IGNORECASE,
        )
    ]

    # ARver's process exit status is the safest overall success indicator.
    verification_successful = (
        return_code == 0
        and database_available
        and not failed_lines
    )

    verified_count = len(verified_lines)
    failed_count = len(failed_lines)
    unavailable_count = len(unavailable_lines)

    # Some versions provide only a final success status rather than one easily
    # parseable line per track.
    if (
        verification_successful
        and verified_count == 0
        and expected_track_count > 0
    ):
        verified_count = expected_track_count

    return AccurateRipStatistics(
        database_available=database_available,
        total_tracks_found=expected_track_count,
        verified_tracks=min(verified_count, expected_track_count),
        failed_tracks=failed_count,
        unavailable_tracks=unavailable_count,
        confidence_values=confidence_values,
        verification_successful=verification_successful,
    )


# ---------------------------------------------------------------------------
# Complete workflow
# ---------------------------------------------------------------------------

def rip_and_verify(
    *,
    device: str,
    output_directory: Path,
    drive_offset: int | None,
    eject_when_finished: bool,
) -> RipReport:
    """
    Run the complete close -> detect -> rip -> verify -> report -> eject flow.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    messages: list[str] = []

    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)

    abcde_log_path = output_directory / "abcde.log"
    accuraterip_log_path = output_directory / "accuraterip.log"
    report_path = output_directory / "rip_report.json"

    abcde_result: CommandResult | None = None
    arver_result: CommandResult | None = None
    tracks: list[Path] = []

    # Temporary abcde WAV files and configuration are isolated from the final
    # output directory.
    with tempfile.TemporaryDirectory(prefix="abcde-rip-") as temporary_root:
        temporary_root_path = Path(temporary_root)
        wav_directory = temporary_root_path / "wav"
        abcde_config = temporary_root_path / "abcde.conf"

        create_abcde_config(
            config_path=abcde_config,
            output_directory=output_directory,
            temporary_directory=wav_directory,
            device=device,
            drive_offset=drive_offset,
        )

        try:
            messages.append(f"Closing optical-drive tray for {device}.")
            close_drive(device)

            messages.append("Waiting for a readable audio CD.")
            wait_for_audio_cd(device)

            messages.append("Starting abcde audio extraction.")
            abcde_result = rip_with_abcde(
                device=device,
                output_directory=output_directory,
                temporary_directory=wav_directory,
                config_path=abcde_config,
            )

            abcde_log_text = (
                "COMMAND:\n"
                + " ".join(abcde_result.command)
                + "\n\nSTDOUT:\n"
                + abcde_result.stdout
                + "\n\nSTDERR:\n"
                + abcde_result.stderr
            )
            abcde_log_path.write_text(abcde_log_text, encoding="utf-8")

            tracks = find_ripped_tracks(output_directory)

            if not abcde_result.successful:
                messages.append(
                    f"abcde failed with return code "
                    f"{abcde_result.return_code}."
                )
            elif not tracks:
                messages.append(
                    "abcde returned success, but no FLAC files were found."
                )
            else:
                messages.append(
                    f"abcde created {len(tracks)} FLAC track(s)."
                )

                messages.append(
                    "Starting AccurateRip verification with ARver."
                )
                arver_result = verify_with_arver(device, tracks)

                arver_log_text = (
                    "COMMAND:\n"
                    + " ".join(arver_result.command)
                    + "\n\nSTDOUT:\n"
                    + arver_result.stdout
                    + "\n\nSTDERR:\n"
                    + arver_result.stderr
                )
                accuraterip_log_path.write_text(
                    arver_log_text,
                    encoding="utf-8",
                )

        finally:
            if eject_when_finished:
                try:
                    eject_drive(device)
                    messages.append("Optical-drive tray ejected.")
                except CDOperationError as exc:
                    messages.append(f"Could not eject the tray: {exc}")

    abcde_combined_output = ""
    if abcde_result:
        abcde_combined_output = (
            abcde_result.stdout + "\n" + abcde_result.stderr
        )

    reading_stats = parse_cdparanoia_statistics(abcde_combined_output)

    if arver_result:
        arver_combined_output = (
            arver_result.stdout + "\n" + arver_result.stderr
        )
        accuraterip_stats = parse_accuraterip_statistics(
            arver_combined_output,
            arver_result.return_code,
            len(tracks),
        )
    else:
        accuraterip_stats = AccurateRipStatistics(
            total_tracks_found=len(tracks)
        )

    rip_successful = bool(
        abcde_result
        and abcde_result.successful
        and tracks
        and reading_stats.unrecoverable_events == 0
    )

    finished_at = datetime.now(timezone.utc).isoformat()

    report = RipReport(
        started_at=started_at,
        finished_at=finished_at,
        device=device,
        output_directory=str(output_directory),
        abcde_return_code=(
            abcde_result.return_code if abcde_result else -1
        ),
        accuraterip_return_code=(
            arver_result.return_code if arver_result else None
        ),
        rip_successful=rip_successful,
        verification_successful=(
            accuraterip_stats.verification_successful
        ),
        audio_files=[str(track) for track in tracks],
        reading_statistics=reading_stats,
        accuraterip_statistics=accuraterip_stats,
        messages=messages,
    )

    report_path.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return report


# ---------------------------------------------------------------------------
# Human-readable console output
# ---------------------------------------------------------------------------

def print_report(report: RipReport) -> None:
    """Display the most useful result fields in the terminal."""

    read_stats = report.reading_statistics
    ar_stats = report.accuraterip_statistics

    print("\n" + "=" * 68)
    print("CD RIP AND VERIFICATION REPORT")
    print("=" * 68)

    print(f"Device:                  {report.device}")
    print(f"Output directory:        {report.output_directory}")
    print(f"Audio files produced:    {len(report.audio_files)}")
    print(f"abcde return code:       {report.abcde_return_code}")
    print(f"ARver return code:       {report.accuraterip_return_code}")

    print("\nReading diagnostics")
    print("-" * 68)
    print(
        f"Detected read events:    "
        f"{read_stats.detected_read_events}"
    )
    print(
        f"Corrected/retried:       "
        f"{read_stats.corrected_or_retried_events}"
    )
    print(
        f"Non-recoverable events:  "
        f"{read_stats.unrecoverable_events}"
    )
    print(f"Overlap events:          {read_stats.overlap_events}")
    print(f"Drift events:            {read_stats.drift_events}")
    print(
        f"Dropped/skipped events:  "
        f"{read_stats.dropped_or_skipped_events}"
    )
    print(f"Transport errors:        {read_stats.transport_errors}")

    print("\nAccurateRip")
    print("-" * 68)
    print(f"Database available:      {ar_stats.database_available}")
    print(f"Tracks supplied:         {ar_stats.total_tracks_found}")
    print(f"Verified tracks:         {ar_stats.verified_tracks}")
    print(f"Failed tracks:           {ar_stats.failed_tracks}")
    print(f"Unavailable/N/A:         {ar_stats.unavailable_tracks}")
    print(f"Confidence values:       {ar_stats.confidence_values}")

    print("\nFinal status")
    print("-" * 68)
    print(
        "Rip completed:           "
        + ("YES" if report.rip_successful else "NO")
    )
    print(
        "AccurateRip verified:    "
        + ("YES" if report.verification_successful else "NO")
    )

    if report.messages:
        print("\nMessages")
        print("-" * 68)
        for message in report.messages:
            print(f"- {message}")

    print("\nDetailed files:")
    print(f"- {Path(report.output_directory) / 'abcde.log'}")
    print(f"- {Path(report.output_directory) / 'accuraterip.log'}")
    print(f"- {Path(report.output_directory) / 'rip_report.json'}")


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    """Create and parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Close an optical drive, rip an audio CD with abcde, "
            "verify it with AccurateRip through ARver, and eject it."
        )
    )

    parser.add_argument(
        "--device",
        default="/dev/cdrom",
        help="Optical-drive device. Default: /dev/cdrom",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "Music" / "CD-Rips",
        help="Destination directory for FLAC files and logs.",
    )

    parser.add_argument(
        "--drive-offset",
        type=int,
        default=None,
        help=(
            "Known AccurateRip read offset, in samples. "
            "Example: --drive-offset 6"
        ),
    )

    parser.add_argument(
        "--leave-closed",
        action="store_true",
        help="Do not eject the disc after the operation.",
    )

    parser.add_argument(
        "--eject-only",
        action="store_true",
        help="Only open/eject the optical-drive tray.",
    )

    parser.add_argument(
        "--close-only",
        action="store_true",
        help="Only close the optical-drive tray.",
    )

    return parser.parse_args()


def main2(code:str="next") -> int:
    """Program entry point."""

    args = parse_arguments()

    try:
        require_programs(["eject"])

        if args.eject_only | (code == 'eject'):
            eject_drive(args.device)
            print(f"Ejected {args.device}.")
            return 0

        if args.close_only:
            close_drive(args.device)
            print(f"Closed {args.device}.")
            return 0

        require_programs([
            "abcde",
            "cdparanoia",
            "flac",
            "arver",
        ])

        ppath = args.output
        if code == "timestamp":
            ppath = ppath / datetime.now().strftime("%Y%m%d%H%M%S")

        report = rip_and_verify(
            device=args.device,
            output_directory=ppath,
            drive_offset=args.drive_offset,
            eject_when_finished=not args.leave_closed,
        )

        print_report(report)

        # Return success only when both ripping and AccurateRip verification
        # succeeded.

        if len(report.audio_files) == 0:
                flag = false
                for i in range(len(report.messages)):
                     if report.messages[i] == "Starting abcde audio extraction.":
                                flag = True
                                break

                if not flag:
                        print("ERROR: Seems like no CD was detected.")
                        return 3

        return (
            0
            if report.rip_successful
            and report.verification_successful
            else 1
        )

    except CDOperationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nOperation cancelled by the user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main2())


