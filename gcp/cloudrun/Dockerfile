FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV OES_DB_PATH=/tmp/ops_evidence_synthesis.sqlite3

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
COPY data /app/data
COPY gcp /app/gcp

RUN pip install --no-cache-dir ".[api,gcp]"

EXPOSE 8080
CMD ["uvicorn", "ops_evidence_synthesis.api:app", "--host", "0.0.0.0", "--port", "8080"]
