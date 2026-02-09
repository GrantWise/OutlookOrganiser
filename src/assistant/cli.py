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
from assistant.core.logging import configure_logging, get_logger

if TYPE_CHECKING:
    import anthropic

    from assistant.auth.msal_auth import GraphAuth
    from assistant.classifier.snippet import SnippetCleaner
    from assistant.config_schema import AppConfig
    from assistant.db.store import DatabaseStore
    from assistant.graph.client import GraphClient
    from assistant.graph.folders import FolderManager
    from assistant.graph.messages import MessageManager
    from assistant.graph.tasks import CategoryManager, TaskManager

console = Console()
logger = get_logger(__name__)


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
    task_manager: TaskManager | None = None
    category_manager: CategoryManager | None = None


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
    from assistant.graph.tasks import CategoryManager as _CategoryManager
    from assistant.graph.tasks import TaskManager as _TaskManager

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
    task_manager = _TaskManager(graph_client)
    category_manager = _CategoryManager(graph_client)

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
        task_manager=task_manager,
        category_manager=category_manager,
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
@click.option(
    "--backlog-days",
    type=int,
    default=None,
    help="Classify emails from local DB (last N days) and create suggestions",
)
def triage(once: bool, is_dry_run: bool, backlog_days: int | None) -> None:
    """Run triage engine.

    Without --once, starts the scheduler for continuous operation.
    With --once, runs a single triage cycle and exits.
    With --backlog-days N, classifies DB emails from the last N days
    and creates suggestions for web UI review.
    """
    if backlog_days is not None:
        if is_dry_run:
            console.print("[red]Error:[/red] --backlog-days and --dry-run are mutually exclusive.")
            sys.exit(1)
        if not once:
            console.print("[red]Error:[/red] --backlog-days requires --once.")
            sys.exit(1)
        try:
            asyncio.run(_run_triage_backlog(backlog_days))
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            sys.exit(130)
        except SystemExit:
            raise
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}")
            sys.exit(1)
    elif once:
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
        category_manager=deps.category_manager,
        graph_client=deps.graph_client,
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


async def _run_triage_backlog(days: int) -> None:
    """Classify emails from the local database and create suggestions.

    Loads emails from the last N days (previously saved by bootstrap or triage),
    classifies them, and creates suggestions for review in the web UI.
    Skips emails that already have suggestions.
    """
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
        category_manager=deps.category_manager,
        graph_client=deps.graph_client,
    )

    console.print(
        f"[bold]Backlog triage:[/bold] classifying emails from last {days} days\n"
        "Emails with existing suggestions will be skipped.\n"
    )

    result = await engine.run_backlog_cycle(days)

    # Print summary
    console.print(f"\n[bold]Backlog Triage Summary[/bold] (cycle {result.cycle_id[:8]}...)")
    console.print(f"  Duration:    {result.duration_ms}ms")
    console.print(f"  DB emails:   {result.emails_fetched}")
    console.print(f"  Processed:   {result.emails_processed}")
    console.print(f"  Auto-ruled:  {result.auto_ruled}")
    console.print(f"  Classified:  {result.classified}")
    console.print(f"  Inherited:   {result.inherited}")
    console.print(f"  Skipped:     {result.skipped}")
    console.print(f"  Failed:      {result.failed}")

    suggestions_created = result.auto_ruled + result.classified + result.inherited
    if suggestions_created > 0:
        console.print(
            f"\n[green]{suggestions_created} suggestions created.[/green] "
            "Start the web UI with [cyan]python -m assistant serve[/cyan] to review."
        )
    else:
        console.print("\n[yellow]No new suggestions created.[/yellow]")


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
        category_manager=deps.category_manager,
        graph_client=deps.graph_client,
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


@cli.command("bootstrap-categories")
@click.option(
    "--force",
    is_flag=True,
    help="Re-run bootstrap even if already completed",
)
def bootstrap_categories(force: bool) -> None:
    """Bootstrap Outlook master categories (framework + taxonomy).

    Creates the 10 framework categories (priorities + action types) and
    taxonomy categories (one per project/area in config). Runs interactive
    cleanup of orphaned categories on first run.
    """
    try:
        asyncio.run(_run_bootstrap_categories(force))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_bootstrap_categories(force: bool) -> None:
    """Async implementation of bootstrap-categories command."""
    from assistant.graph.tasks import (
        AREA_CATEGORY_COLOR,
        FRAMEWORK_CATEGORIES,
    )

    deps = await _init_cli_deps()

    if deps.category_manager is None:
        console.print("[red]Error:[/red] Category manager not available (auth failed?).")
        sys.exit(1)

    # Check if already bootstrapped
    already_done = await deps.store.get_state("categories_bootstrapped")
    if already_done == "true" and not force:
        console.print("[yellow]Categories already bootstrapped.[/yellow] Use --force to re-run.")
        return

    console.print("[bold]Bootstrapping Outlook master categories...[/bold]\n")

    # Fetch existing categories
    existing = deps.category_manager.get_categories()
    existing_names = {cat["displayName"] for cat in existing}

    created_count = 0
    skipped_count = 0

    # 1. Framework categories (10 total)
    console.print("[cyan]Framework categories:[/cyan]")
    for name, color in FRAMEWORK_CATEGORIES.items():
        if name in existing_names:
            console.print(f"  [dim]✓ {name} (exists, color preserved)[/dim]")
            skipped_count += 1
        else:
            deps.category_manager.create_category(name, color)
            console.print(f"  [green]+ {name}[/green] ({color})")
            created_count += 1

    # 2. Area taxonomy categories (projects excluded -- they're temporary
    # and the folder hierarchy already conveys the project)
    console.print("\n[cyan]Area taxonomy categories:[/cyan]")
    for area in deps.config.areas:
        if area.name in existing_names:
            console.print(f"  [dim]✓ {area.name} (exists)[/dim]")
            skipped_count += 1
        else:
            deps.category_manager.create_category(area.name, AREA_CATEGORY_COLOR)
            console.print(f"  [green]+ {area.name}[/green] (area)")
            created_count += 1

    console.print(
        f"\n[bold]Summary:[/bold] {created_count} created, {skipped_count} already existed"
    )

    # 3. Interactive cleanup of orphaned categories
    # Managed = framework categories + area taxonomy (not projects)
    managed_names = set(FRAMEWORK_CATEGORIES.keys())
    for area in deps.config.areas:
        managed_names.add(area.name)

    # Re-fetch after creates to get full list
    all_categories = deps.category_manager.get_categories()
    orphans = [cat for cat in all_categories if cat["displayName"] not in managed_names]

    if orphans:
        console.print(f"\n[yellow]Found {len(orphans)} unmanaged categories:[/yellow]")
        for i, cat in enumerate(orphans, 1):
            console.print(f"  {i}. {cat['displayName']} ({cat.get('color', 'none')})")

        choice = click.prompt(
            "\nDelete these categories? (y=all, n=skip, or comma-separated numbers)",
            default="n",
        )

        if choice.lower() == "y":
            for cat in orphans:
                deps.category_manager.delete_category(cat["id"])
                console.print(f"  [red]- {cat['displayName']}[/red]")
            console.print(f"  Deleted {len(orphans)} orphaned categories.")
        elif choice.lower() != "n":
            # Parse comma-separated indices
            try:
                indices = [int(x.strip()) for x in choice.split(",")]
                for idx in indices:
                    if 1 <= idx <= len(orphans):
                        cat = orphans[idx - 1]
                        deps.category_manager.delete_category(cat["id"])
                        console.print(f"  [red]- {cat['displayName']}[/red]")
            except ValueError:
                console.print("[yellow]Invalid selection, skipping cleanup.[/yellow]")
    else:
        console.print("\n[dim]No orphaned categories found.[/dim]")

    # Mark as bootstrapped
    await deps.store.set_state("categories_bootstrapped", "true")
    console.print("\n[green]✓ Category bootstrap complete.[/green]")


@cli.command("migrate-immutable-ids")
def migrate_immutable_ids() -> None:
    """Run the one-time mutable-to-immutable ID migration.

    Fetches each stored email ID with the Prefer: IdType="ImmutableId" header
    and updates the database if the ID changed.
    """
    try:
        asyncio.run(_run_migrate_immutable_ids())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_migrate_immutable_ids() -> None:
    """Async implementation of immutable ID migration."""
    deps = await _init_cli_deps()
    await _migrate_to_immutable_ids(deps.store, deps.graph_client, console)


async def _migrate_to_immutable_ids(store, graph_client, output_console=None) -> None:
    """Migrate stored email IDs from mutable to immutable format.

    Called from both CLI command and serve lifespan.

    Args:
        store: DatabaseStore instance
        graph_client: GraphClient instance (already sends Prefer header)
        output_console: Optional Rich Console for CLI output
    """
    from assistant.core.errors import GraphAPIError as _GraphAPIError

    migrated_key = await store.get_state("immutable_ids_migrated")
    if migrated_key == "true":
        if output_console:
            output_console.print("[dim]Immutable IDs already migrated.[/dim]")
        return

    all_ids = await store.get_all_email_ids()
    if not all_ids:
        await store.set_state("immutable_ids_migrated", "true")
        if output_console:
            output_console.print("[dim]No emails to migrate.[/dim]")
        return

    if output_console:
        output_console.print(
            f"[bold]Migrating {len(all_ids)} email IDs to immutable format...[/bold]"
        )

    migrated = 0
    skipped = 0
    not_found = 0

    for old_id in all_ids:
        try:
            # Fetch message with immutable ID header (already in _get_headers)
            msg = graph_client.get(
                f"/me/messages/{old_id}",
                params={"$select": "id"},
            )
            new_id = msg.get("id", old_id)
            if new_id != old_id:
                await store.update_email_id(old_id, new_id)
                migrated += 1
            else:
                skipped += 1
        except _GraphAPIError as e:
            if e.status_code == 404:
                not_found += 1
                logger.warning(
                    "immutable_id_migration_404",
                    old_id=old_id[:20] + "...",
                )
            else:
                logger.warning(
                    "immutable_id_migration_error",
                    old_id=old_id[:20] + "...",
                    error=str(e),
                )
                skipped += 1

    await store.set_state("immutable_ids_migrated", "true")

    summary = f"Migrated {migrated} IDs, {skipped} unchanged, {not_found} not found (deleted)"
    logger.info("immutable_id_migration_complete", summary=summary)
    if output_console:
        output_console.print(f"[green]✓ {summary}[/green]")


@cli.command("rules")
@click.option(
    "--audit",
    is_flag=True,
    help="Run auto-rules health audit",
)
def rules(audit: bool) -> None:
    """Manage auto-rules.

    With --audit, checks for conflicts, stale rules, and rule count limits.
    """
    if audit:
        try:
            asyncio.run(_run_rules_audit())
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            sys.exit(130)
        except SystemExit:
            raise
        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}")
            sys.exit(1)
    else:
        console.print("Use [cyan]--audit[/cyan] to run auto-rules health check.")


async def _run_rules_audit() -> None:
    """Async implementation of rules audit command."""
    from assistant.classifier.auto_rules import audit_report

    deps = await _init_cli_deps()

    match_counts = await deps.store.get_auto_rule_match_counts()
    report = audit_report(
        rules=deps.config.auto_rules,
        match_counts=match_counts,
        max_rules=deps.config.auto_rules_hygiene.max_rules,
        threshold_days=deps.config.auto_rules_hygiene.consolidation_check_days,
    )

    console.print("[bold]Auto-Rules Audit Report[/bold]\n")
    console.print(f"  Total rules: {report.total_rules} / {report.max_rules}")

    if report.over_limit:
        console.print(
            f"  [red]WARNING: Over limit ({report.total_rules} > {report.max_rules})[/red]"
        )

    if report.conflicts:
        console.print(f"\n  [yellow]Conflicts ({len(report.conflicts)}):[/yellow]")
        for c in report.conflicts:
            console.print(f"    - {c.rule_a} <-> {c.rule_b} ({c.overlap_type} overlap)")
    else:
        console.print("\n  [green]No conflicts detected.[/green]")

    if report.stale_rules:
        console.print(f"\n  [yellow]Stale rules ({len(report.stale_rules)}):[/yellow]")
        for name in report.stale_rules:
            console.print(f"    - {name}")
    else:
        console.print("  [green]No stale rules.[/green]")

    console.print()


@cli.command("digest")
@click.option(
    "--stdout",
    "delivery",
    flag_value="stdout",
    default=True,
    help="Print digest to stdout (default)",
)
@click.option(
    "--file",
    "delivery",
    flag_value="file",
    help="Write digest to data/ directory",
)
def digest(delivery: str) -> None:
    """Generate a daily digest report.

    Summarizes overdue replies, waiting-for items, processing stats,
    and pending suggestions.
    """
    try:
        asyncio.run(_run_digest(delivery))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        sys.exit(1)


async def _run_digest(delivery: str) -> None:
    """Async implementation of digest command."""
    import anthropic as anthropic_mod

    from assistant.engine.digest import DigestGenerator

    deps = await _init_cli_deps()

    async_client = anthropic_mod.AsyncAnthropic(max_retries=3)
    generator = DigestGenerator(
        store=deps.store,
        anthropic_client=async_client,
        config=deps.config,
    )

    result = await generator.generate()
    await generator.deliver(result, mode=delivery)


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
