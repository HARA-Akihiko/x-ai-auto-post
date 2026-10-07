FROM python:3.12-slim
WORKDIR /srv/app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.23 \
    && uv sync --frozen --no-dev
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN useradd --uid 10001 --create-home app
USER app
ENV PATH="/srv/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
