FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -m app \
    && chown -R app:app /app
COPY --chown=app:app . .
ENV PORT=5000
EXPOSE 5000
USER app
# exec-form via sh: `exec` replaces the shell so gunicorn becomes PID 1 and
# receives SIGTERM directly (Deplexo requirement); ${PORT:-5000} keeps
# compatibility with hosts that assign a dynamic $PORT (e.g. Render).
CMD ["sh", "-c", "exec gunicorn --workers 1 --timeout 120 --bind 0.0.0.0:${PORT:-5000} app:app"]
