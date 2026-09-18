FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
    && pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 compost
COPY app app
COPY ml_runtime ml_runtime
COPY ml_service.py .
COPY alembic.ini .
COPY alembic alembic
RUN chown -R compost:compost /app
USER compost
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
