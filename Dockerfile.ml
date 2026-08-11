FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
WORKDIR /app
COPY requirements.txt requirements-ml-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-ml-runtime.txt
RUN useradd --create-home --uid 10001 compost
COPY app app
COPY ml_runtime ml_runtime
COPY ml_service.py .
RUN chown -R compost:compost /app
USER compost
CMD ["uvicorn", "ml_service:app", "--host", "0.0.0.0", "--port", "8001"]
