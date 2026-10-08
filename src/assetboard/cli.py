from __future__ import annotations

import typer

from assetboard.app import create_app
from assetboard.branding import APP_NAME, CLI_NAME
from assetboard.config import Settings

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
    """Validate the application configuration and database availability."""
    settings = Settings()
    create_app(settings)
    typer.echo("Migration check complete")


if __name__ == "__main__":
    app()
