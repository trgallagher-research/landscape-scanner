"""Command-line interface.

    scanner run --question "..." [--geography Kenya ...] [--budget 2.0]
                [--profile economy|quality] [--out report.json]
    scanner keys                 # show which provider keys are set (never values)

The web UI (``scanner ui``) is the friendlier front door; the CLI exists
for scripted runs and for testing the engine without a browser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .keys import KNOWN_PROVIDERS, ProviderKeys
from .models import RunConfig
from .pipeline import RunHalted, build_pipeline


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``scanner`` command."""
    parser = argparse.ArgumentParser(
        prog="scanner",
        description="Verification-grounded landscape analysis.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a landscape scan.")
    run_parser.add_argument("--question", required=True, help="The landscape question, in plain English.")
    run_parser.add_argument("--geography", nargs="*", default=[], help="Geographic focus, e.g. Kenya.")
    run_parser.add_argument("--budget", type=float, default=2.0, help="Hard spend cap in USD (default 2.0).")
    run_parser.add_argument(
        "--model-profile", choices=["economy", "quality"], default="economy", dest="model_profile",
        help="economy = cheap extraction models + Claude verification (default).",
    )
    run_parser.add_argument("--shortlist", type=int, default=25, help="Entities to deep-profile (default 25).")
    run_parser.add_argument("--out", default="", help="Write the report JSON here (default: runs/<id>/report.json only).")

    subparsers.add_parser("keys", help="Show which provider keys are configured (never shows values).")

    args = parser.parse_args(argv)

    if args.command == "keys":
        return _cmd_keys()
    if args.command == "run":
        return _cmd_run(args)
    return 2


def _cmd_keys() -> int:
    """Print set/missing status for every known provider."""
    keys = ProviderKeys()
    print("Provider keys (values are never displayed):")
    for provider, ok in keys.statuses().items():
        status = "SET" if ok else "missing"
        role = KNOWN_PROVIDERS[provider]["role"]
        print(f"  {provider:<12} {status:<8} {role}")
    missing = keys.missing_for_live_run()
    if missing:
        print(f"\nA live run needs: {', '.join(missing)} — set them in .env first.")
    else:
        print("\nReady for a live run.")
    return 0


def _cmd_run(args) -> int:
    """Run one scan end to end and print the summary."""
    config = RunConfig(
        question=args.question,
        geography=args.geography,
        budget_usd=args.budget,
        model_profile=args.model_profile,
        shortlist_size=args.shortlist,
    )
    keys = ProviderKeys()
    try:
        pipeline = build_pipeline(config, keys, runs_dir=Path("runs"))
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Run directory: {pipeline.store.run_dir}")
    print(f"Budget: ${config.budget_usd:.2f}  Profile: {config.model_profile}")
    try:
        report = pipeline.run()
    except RunHalted as halt:
        print(f"\nHALTED (resumable): {halt}", file=sys.stderr)
        print("Re-run the same command with a higher --budget to continue.", file=sys.stderr)
        return 3

    # Persist where asked (.html -> shareable report, otherwise JSON),
    # then print the human summary.
    if args.out:
        out_path = Path(args.out)
        if out_path.suffix.lower() in (".html", ".htm"):
            from .report_html import render_html

            out_path.write_text(render_html(report), encoding="utf-8")
        else:
            out_path.write_text(report.model_dump_json(indent=1), encoding="utf-8")
        print(f"Report written to {out_path}")

    overview = report.overview
    print(f"\n=== {overview.headline} ===")
    print(f"Entities deep-profiled: {len(report.entities)}   Long tail: {len(report.long_tail)}")
    print(f"Confidence: {overview.confidence_counts}")
    if overview.key_players:
        print(f"Key players: {', '.join(overview.key_players[:8])}")
    if overview.gaps:
        print("Gaps:")
        for gap in overview.gaps:
            print(f"  - {gap}")
    print(f"\nCost: ${report.cost.total_usd:.2f} across {report.cost.llm_calls} model calls")
    for note in report.coverage_notes:
        print(f"NOTE: {note}")
    for note in report.manifest.notes:
        print(f"MANIFEST: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
