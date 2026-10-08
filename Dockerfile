FROM python:3.12-slim

WORKDIR /app

# PyNaCl for Ed25519 identity seats (same lib as Chamber)
RUN pip install --no-cache-dir pynacl

COPY identity/ ./identity/
COPY chat/ ./chat/
COPY spend/ ./spend/

ENV PORT=8080
ENV RIDERXL_HOST=0.0.0.0
# Never run with the insecure permissions stub in production
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-c", "import time; print('debug-sleep', flush=True); time.sleep(600)"]
