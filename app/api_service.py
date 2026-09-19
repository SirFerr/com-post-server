"""ASGI entrypoint for the JSON and administrative API."""

from .main import create_app

app = create_app(role="api")
