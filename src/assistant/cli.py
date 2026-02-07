"""Command-line interface for the Outlook AI Assistant.

Provides commands for configuration validation, bootstrap, triage, and server.

Usage:
    python -m assistant validate-config
    python -m assistant bootstrap --days 90
    python -m assistant dry-run --days 90 --sample 20
    python -m assistant serve
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console

from assistant.config import validate_config_file
from assistant.core.logging import configure_logging

if TYPE_CHECKING:
    import anthropic

    from assistant.auth.msal_auth import GraphAuth
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore
    from assistant.graph.client import GraphClient
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager

console = Console()


@dataclass(frozen=True, slots=True)
class CLIDeps:
    """Shared dependencies initialized by _init_cli_deps()."""

    config: AppConfig
    auth: GraphAuth
    graph_client: GraphClient
    message_manager: MessageManager
    folder_manager: FolderManager
    store: DatabaseStore
    anthropic_client: anthropic.Anthropic
    snippet_cleaner: SnippetCleaner


async def _init_cli_deps() -> CLIDeps:
    """Initialize shared CLI dependencies.

    Loads config, initializes auth/Graph/DB/Anthropic/snippet cleaner,
    and returns them in a frozen dataclass. Prints actionable error
    messages and calls sys.exit(1) on failure.
    """
    import anthropic as anthropic_mod

    from assistant.auth.msal_auth import GraphAuth
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config import get_config
    from assistant.core.errors import AuthenticationError, ConfigLoadError
    from assistant.db.store import DatabaseStore
    from assistant.graph.client import GraphClient
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager

    # 1. Load config
    try:
        config = get_config()
    except (ConfigLoadError, Exception) as e:
        console.print(
            f"[red]Config error:[/red] {e}\n\n"
            "Create config/config.yaml with at least an [cyan]auth[/cyan] section.\n"
            "See Reference/spec/07-setup-guide.md for setup instructions."
        )
        sys.exit(1)

    # 2. Initialize auth
    try:
        auth = GraphAuth(
            client_id=config.auth.client_id,
            tenant_id=config.auth.tenant_id,
            scopes=config.auth.scopes,
            token_cache_path=config.auth.token_cache_path,
        )
    except AuthenticationError as e:
        console.print(
            f"[red]Authentication error:[/red] {e}\n\n"
            "Check your Azure AD app registration and try again."
        )
        sys.exit(1)

    # 3. Initialize Graph client and managers
    graph_client = GraphClient(auth)
    message_manager = MessageManager(graph_client)
    folder_manager = FolderManager(graph_client)

    # 4. Initialize database
    db_path = Path("data/assistant.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DatabaseStore(db_path)
    await store.initialize()

    # 5. Initialize Anthropic client and snippet cleaner
    anthropic_client = anthropic_mod.Anthropic(max_retries=3)
    snippet_cleaner = SnippetCleaner(max_length=config.snippet.max_length)

    return CLIDeps(
        config=config,
        auth=auth,
        graph_client=graph_client,
        message_manager=message_manager,
        folder_manager=folder_manager,
        store=store,
        anthropic_client=anthropic_client,
        snippet_cleaner=snippet_cleaner,
    )


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
    import uvicorn

    from assistant.web.app import create_app

    if host == "0.0.0.0":  # noqa: S104
        console.print(
            "[yellow]Warning:[/yellow] Binding to 0.0.0.0 exposes the server to the network.\n"
            "This app has no authentication. Use 127.0.0.1 for local-only access."
        )

    configure_logging(log_level="INFO", json_output=True)

    app = create_app()
    console.print(f"Starting server on [cyan]http://{host}:{port}[/cyan]")
    uvicorn.run(app, host=host, port=port, log_level="info")


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
    try:
        asyncio.run(_run_bootstrap(days, force))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_bootstrap(days: int, force: bool) -> None:
    """Async implementation of bootstrap command."""
    from assistant.core.errors import ClassificationError
    from assistant.engine.bootstrap import BootstrapEngine

    deps = await _init_cli_deps()

    engine = BootstrapEngine(
        anthropic_client=deps.anthropic_client,
        message_manager=deps.message_manager,
        folder_manager=deps.folder_manager,
        store=deps.store,
        snippet_cleaner=deps.snippet_cleaner,
        config=deps.config,
        console=console,
    )

    try:
        await engine.run(days=days, force=force)
    except ClassificationError as e:
        console.print(
            f"\n[red]Classification error:[/red] {e}\n\n"
            "Check your ANTHROPIC_API_KEY environment variable."
        )
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
    try:
        asyncio.run(_run_dry_run(days, sample, limit))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_dry_run(days: int, sample: int, limit: int | None) -> None:
    """Async implementation of dry-run command."""
    from assistant.classifier.claude_classifier import EmailClassifier
    from assistant.engine.dry_run import DryRunEngine
    from assistant.engine.thread_utils import ThreadContextManager

    deps = await _init_cli_deps()

    if not deps.config.projects and not deps.config.areas:
        console.print(
            "[yellow]Warning:[/yellow] No projects or areas configured. "
            "Dry-run results will be limited.\n"
            "Run bootstrap first, then edit config/config.yaml.proposed and rename to config.yaml."
        )

    thread_manager = ThreadContextManager(
        store=deps.store,
        message_manager=deps.message_manager,
        snippet_cleaner=deps.snippet_cleaner,
    )
    classifier = EmailClassifier(
        anthropic_client=deps.anthropic_client,
        store=deps.store,
        config=deps.config,
    )

    engine = DryRunEngine(
        classifier=classifier,
        store=deps.store,
        message_manager=deps.message_manager,
        snippet_cleaner=deps.snippet_cleaner,
        thread_manager=thread_manager,
        config=deps.config,
        console=console,
    )

    await engine.run(days=days, sample=sample, limit=limit)


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
    if once:
        try:
            asyncio.run(_run_triage_once(is_dry_run))
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            sys.exit(130)
        except SystemExit:
            raise
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}")
            sys.exit(1)
    else:
        # Continuous mode: start the web server with scheduler
        console.print("Starting triage in continuous mode (use 'serve' for UI + triage)...")
        try:
            asyncio.run(_run_triage_continuous(is_dry_run))
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/yellow]")
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}")
            sys.exit(1)


async def _run_triage_once(is_dry_run: bool) -> None:
    """Run a single triage cycle and print results."""
    from assistant.classifier.claude_classifier import EmailClassifier
    from assistant.engine.thread_utils import ThreadContextManager
    from assistant.engine.triage import TriageEngine
    from assistant.graph.messages import SentItemsCache

    deps = await _init_cli_deps()

    thread_manager = ThreadContextManager(
        store=deps.store,
        message_manager=deps.message_manager,
        snippet_cleaner=deps.snippet_cleaner,
    )
    classifier = EmailClassifier(
        anthropic_client=deps.anthropic_client,
        store=deps.store,
        config=deps.config,
    )
    sent_cache = SentItemsCache(deps.message_manager)

    engine = TriageEngine(
        classifier=classifier,
        store=deps.store,
        message_manager=deps.message_manager,
        folder_manager=deps.folder_manager,
        snippet_cleaner=deps.snippet_cleaner,
        thread_manager=thread_manager,
        sent_cache=sent_cache,
        config=deps.config,
    )

    if is_dry_run:
        console.print("[cyan]Dry-run mode:[/cyan] suggestions will not be created\n")

    result = await engine.run_cycle()

    # Print summary
    console.print(f"\n[bold]Triage Cycle Summary[/bold] (cycle {result.cycle_id[:8]}...)")
    console.print(f"  Duration:    {result.duration_ms}ms")
    console.print(f"  Fetched:     {result.emails_fetched}")
    console.print(f"  Processed:   {result.emails_processed}")
    console.print(f"  Auto-ruled:  {result.auto_ruled}")
    console.print(f"  Classified:  {result.classified}")
    console.print(f"  Inherited:   {result.inherited}")
    console.print(f"  Skipped:     {result.skipped}")
    console.print(f"  Failed:      {result.failed}")
    if result.degraded_mode:
        console.print("  [yellow]Degraded mode: auto-rules only[/yellow]")


async def _run_triage_continuous(is_dry_run: bool) -> None:
    """Run triage engine in continuous mode with APScheduler."""
    import signal

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from assistant.classifier.claude_classifier import EmailClassifier
    from assistant.engine.thread_utils import ThreadContextManager
    from assistant.engine.triage import TriageEngine
    from assistant.graph.messages import SentItemsCache

    deps = await _init_cli_deps()

    thread_manager = ThreadContextManager(
        store=deps.store,
        message_manager=deps.message_manager,
        snippet_cleaner=deps.snippet_cleaner,
    )
    classifier = EmailClassifier(
        anthropic_client=deps.anthropic_client,
        store=deps.store,
        config=deps.config,
    )
    sent_cache = SentItemsCache(deps.message_manager)

    engine = TriageEngine(
        classifier=classifier,
        store=deps.store,
        message_manager=deps.message_manager,
        folder_manager=deps.folder_manager,
        snippet_cleaner=deps.snippet_cleaner,
        thread_manager=thread_manager,
        sent_cache=sent_cache,
        config=deps.config,
    )

    async def run_cycle():
        result = await engine.run_cycle()
        console.print(
            f"[dim]Cycle {result.cycle_id[:8]}...[/dim] "
            f"fetched={result.emails_fetched} classified={result.classified} "
            f"failed={result.failed} ({result.duration_ms}ms)"
        )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=deps.config.triage.interval_minutes,
        id="triage_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    console.print(
        f"Triage engine running every {deps.config.triage.interval_minutes} minutes. "
        "Press Ctrl+C to stop."
    )

    # Wait until interrupted
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    await stop_event.wait()

    scheduler.shutdown(wait=False)


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
