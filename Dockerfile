FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -m app \
    && chown -R app:app /app
COPY --chown=app:app . .
ENV PORT=5000
# Deplexo runs containers with a read-only root FS: gunicorn's control server
# wants $HOME/.gunicorn, so point HOME at the writable tmpfs.
ENV HOME=/tmp
EXPOSE 5000
# Deplexo mounts its persistent named volume at /data (root-owned by default).
# Pre-create it owned by `app` so the image's directory (with this ownership)
# seeds the volume on first mount — otherwise SQLite gets `permission denied`
# at boot when VM_PILOT_DB=/data/vm_pilot.db.
RUN mkdir -p /data && chown app:app /data
USER app
# exec-form via sh: `exec` replaces the shell so gunicorn becomes PID 1 and
# receives SIGTERM directly (Deplexo requirement); ${PORT:-5000} keeps
# compatibility with hosts that assign a dynamic $PORT (e.g. Render).
CMD ["sh", "-c", "exec gunicorn --workers 1 --timeout 120 --bind 0.0.0.0:${PORT:-5000} app:app"]
