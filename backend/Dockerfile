FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
COPY app ./app
COPY sample_data ./sample_data
# Read from the backend folder at run time (config.py BACKEND_DIR). sector_map.json is personal and
# git-ignored: the [n] pattern copies it when present and skips it in a fresh clone.
COPY fundamentals_aliases.json backtest_criteria.json holdout_criteria.json sector_map.jso[n] ./
RUN uv pip install --system --no-cache ".[postgres,kite,mcp,ai,fundamentals,ocr]"
# /app/data holds the price history, the instrument list and the validated-rules file (written by the
# app): a volume in docker-compose.yml, so a rebuild doesn't lose them.
VOLUME /app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
