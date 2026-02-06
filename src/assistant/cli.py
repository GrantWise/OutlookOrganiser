"""Command-line interface for the Outlook AI Assistant.

Provides commands for configuration validation, bootstrap, triage, and server.

Usage:
    python -m assistant validate-config
    python -m assistant bootstrap --days 90
    python -m assistant dry-run --days 90 --sample 20
    python -m assistant serve
"""

import asyncio
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
    """Async implementation of bootstrap command.

    Initializes all dependencies and runs the bootstrap engine.
    """
    from assistant.auth.msal_auth import GraphAuth
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config import get_config
    from assistant.core.errors import AuthenticationError, ClassificationError, ConfigLoadError
    from assistant.db.store import DatabaseStore
    from assistant.engine.bootstrap import BootstrapEngine
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

    # 5. Initialize Anthropic client
    import anthropic

    anthropic_client = anthropic.Anthropic(max_retries=3)

    # 6. Initialize snippet cleaner
    snippet_cleaner = SnippetCleaner(max_length=config.snippet.max_length)

    # 7. Create and run bootstrap engine
    engine = BootstrapEngine(
        anthropic_client=anthropic_client,
        message_manager=message_manager,
        folder_manager=folder_manager,
        store=store,
        snippet_cleaner=snippet_cleaner,
        config=config,
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
    """Async implementation of dry-run command.

    Initializes all dependencies and runs the dry-run engine.
    """
    from assistant.auth.msal_auth import GraphAuth
    from assistant.classifier.claude_classifier import EmailClassifier
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config import get_config
    from assistant.core.errors import AuthenticationError, ConfigLoadError
    from assistant.db.store import DatabaseStore
    from assistant.engine.dry_run import DryRunEngine
    from assistant.engine.thread_utils import ThreadContextManager
    from assistant.graph.client import GraphClient
    from assistant.graph.messages import MessageManager

    # 1. Load config (must have projects/areas for meaningful dry-run)
    try:
        config = get_config()
    except (ConfigLoadError, Exception) as e:
        console.print(
            f"[red]Config error:[/red] {e}\n\n"
            "Run bootstrap first to generate a config, then edit and rename to config.yaml."
        )
        sys.exit(1)

    if not config.projects and not config.areas:
        console.print(
            "[yellow]Warning:[/yellow] No projects or areas configured. "
            "Dry-run results will be limited.\n"
            "Run bootstrap first, then edit config/config.yaml.proposed and rename to config.yaml."
        )

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

    # 4. Initialize database
    db_path = Path("data/assistant.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DatabaseStore(db_path)
    await store.initialize()

    # 5. Initialize Anthropic client
    import anthropic

    anthropic_client = anthropic.Anthropic(max_retries=3)

    # 6. Initialize components
    snippet_cleaner = SnippetCleaner(max_length=config.snippet.max_length)
    thread_manager = ThreadContextManager(
        store=store,
        message_manager=message_manager,
        snippet_cleaner=snippet_cleaner,
    )
    classifier = EmailClassifier(
        anthropic_client=anthropic_client,
        store=store,
        config=config,
    )

    # 7. Create and run dry-run engine
    engine = DryRunEngine(
        classifier=classifier,
        store=store,
        message_manager=message_manager,
        snippet_cleaner=snippet_cleaner,
        thread_manager=thread_manager,
        config=config,
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
