"""Command-line interface for the Outlook AI Assistant.

Provides commands for configuration validation, bootstrap, triage, and server.

Usage:
    python -m assistant validate-config
    python -m assistant serve
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from assistant.config import validate_config_file
from assistant.core.logging import configure_logging

console = Console()


@click.group()
@click.option("--debug/--no-debug", default=False, help="Enable debug logging")
def cli(debug: bool) -> None:
    """Outlook AI Assistant - AI-powered email management."""
    log_level = "DEBUG" if debug else "INFO"
    # Use human-readable output for CLI, JSON for server
    configure_logging(log_level=log_level, json_output=False)


@cli.command("validate-config")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to config file (default: config/config.yaml)",
)
def validate_config(config_path: Path | None) -> None:
    """Validate the configuration file.

    Checks that config.yaml exists and passes Pydantic schema validation.
    Reports specific errors for invalid fields.
    """
    if config_path:
        console.print(f"Validating config: [cyan]{config_path}[/cyan]")
    else:
        console.print("Validating config: [cyan]config/config.yaml[/cyan]")

    is_valid, message = validate_config_file(config_path)

    if is_valid:
        console.print(f"\n[green]✓[/green] {message}")
        sys.exit(0)
    else:
        console.print(f"\n[red]✗[/red] {message}")
        sys.exit(1)


@cli.command("serve")
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to (default: localhost only for security)",
)
@click.option(
    "--port",
    default=8000,
    type=int,
    help="Port to bind to",
)
def serve(host: str, port: int) -> None:
    """Start the triage scheduler and web UI server.

    Launches the background triage engine and FastAPI web interface.
    """
    console.print("[yellow]serve command not yet implemented (Phase 7)[/yellow]")
    console.print(f"Would start server on {host}:{port}")
    sys.exit(1)


@cli.command("bootstrap")
@click.option(
    "--days",
    default=90,
    type=int,
    help="Number of days to analyze",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip confirmation prompts",
)
def bootstrap(days: int, force: bool) -> None:
    """Run bootstrap scanner to analyze mailbox and generate config.

    Analyzes emails from the last N days to discover projects, areas,
    and sender patterns. Writes proposed config to config.yaml.proposed.
    """
    console.print("[yellow]bootstrap command not yet implemented (Phase 6)[/yellow]")
    console.print(f"Would analyze last {days} days")
    sys.exit(1)


@cli.command("dry-run")
@click.option(
    "--days",
    default=90,
    type=int,
    help="Number of days to analyze",
)
@click.option(
    "--sample",
    default=20,
    type=int,
    help="Number of sample classifications to show",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Maximum emails to process",
)
def dry_run(days: int, sample: int, limit: int | None) -> None:
    """Run classification in dry-run mode (no suggestions created).

    Classifies emails without creating database suggestions.
    Shows folder distribution and sample classifications.
    """
    console.print("[yellow]dry-run command not yet implemented (Phase 6)[/yellow]")
    console.print(f"Would analyze last {days} days, show {sample} samples")
    sys.exit(1)


@cli.command("triage")
@click.option(
    "--once",
    is_flag=True,
    help="Run a single triage cycle and exit",
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    help="Don't create suggestions",
)
def triage(once: bool, is_dry_run: bool) -> None:
    """Run triage engine.

    Without --once, starts the scheduler for continuous operation.
    With --once, runs a single triage cycle and exits.
    """
    console.print("[yellow]triage command not yet implemented (Phase 7)[/yellow]")
    mode = "single cycle" if once else "continuous"
    dry_str = " (dry-run)" if is_dry_run else ""
    console.print(f"Would run triage in {mode} mode{dry_str}")
    sys.exit(1)


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
