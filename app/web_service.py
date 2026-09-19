"""ASGI entrypoint for the staff web interface."""

from .main import create_app

app = create_app(role="web")
