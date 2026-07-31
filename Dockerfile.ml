FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-ml-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-ml-runtime.txt
COPY app app
COPY ml_service.py .
CMD ["uvicorn", "ml_service:app", "--host", "0.0.0.0", "--port", "8001"]
