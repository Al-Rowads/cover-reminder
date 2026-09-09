FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home reminder \
    && mkdir /data \
    && chown reminder:reminder /data
ENV DATABASE_PATH=/data/reminders.sqlite3
USER reminder
ENTRYPOINT ["python", "-m", "cover_reminder"]
CMD ["run"]
