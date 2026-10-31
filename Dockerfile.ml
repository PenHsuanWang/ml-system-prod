FROM python:3.12-slim AS api
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md alembic.ini server.py worker.py manage.py ./
COPY src ./src
COPY bootstrap ./bootstrap
COPY migrations ./migrations
RUN pip install --no-cache-dir '.[observability]' && useradd --uid 10001 --create-home mlapp && mkdir -p var && chown mlapp:mlapp var
USER mlapp
CMD ["uvicorn", "bootstrap.ml_api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "64"]

FROM api AS training
USER root
RUN pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir 'mlflow-skinny==3.6.0' 'numpy>=1.26,<3'
ENV OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MLFLOW_HTTP_REQUEST_TIMEOUT=5 MLFLOW_HTTP_REQUEST_MAX_RETRIES=1
USER mlapp
CMD ["taskiq", "worker", "bootstrap.ml_worker:broker", "--workers", "1", "--max-async-tasks", "1", "--ack-type", "manual"]

FROM training AS acceptance
USER root
COPY tests ./tests
COPY scripts ./scripts
RUN pip install --no-cache-dir '.[dev,training,observability]'
USER mlapp
CMD ["python", "scripts/verify_ml_acceptance.py", "--require-stack", "--output-dir", "/app/var/acceptance"]

FROM training AS ebm
USER root
RUN pip install --no-cache-dir 'scipy>=1.13,<2'
USER mlapp
CMD ["python", "-m", "bootstrap.ebm_cli", "smoke", "--output", "/app/var/ebm-smoke"]

FROM python:3.12-slim AS serving
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
RUN pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir 'numpy>=1.26,<3' 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' 'pydantic>=2.8,<3' 'httpx>=0.27,<1' 'PyJWT[crypto]>=2.10,<3' 'pymongo>=4.13,<5' 'mlflow-skinny==3.6.0' 'opentelemetry-sdk>=1.28,<2' 'opentelemetry-exporter-otlp-proto-http>=1.28,<2'
COPY src ./src
COPY bootstrap ./bootstrap
RUN useradd --uid 10001 --create-home mlapp
USER mlapp
CMD ["python", "-m", "bootstrap.ml_serving", "--host", "0.0.0.0", "--port", "8001"]
