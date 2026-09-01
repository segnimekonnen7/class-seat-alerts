FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies in their own layer so application edits rebuild in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One image runs the API, the worker, and beat -- they differ only by command.
# That means the worker cannot drift from the code the API was tested against.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
