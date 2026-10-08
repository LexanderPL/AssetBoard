from __future__ import annotations

import typer

from assetboard.app import create_app
from assetboard.branding import APP_NAME, CLI_NAME
from assetboard.config import Settings
from assetboard.db.migrations import upgrade_database

app = typer.Typer(name=CLI_NAME, help=f"{APP_NAME} command-line interface")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Host interface to bind."),
    port: int = typer.Option(8000, help="Port to listen on."),
) -> None:
    """Start the development web server."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def migrate() -> None:
    """Run database migrations."""
    settings = Settings()
    if settings.database_url is None:
        raise typer.BadParameter("DATABASE_URL was not configured")
    upgrade_database(settings.database_url)
    typer.echo("Database migrations applied.")


if __name__ == "__main__":
    app()
