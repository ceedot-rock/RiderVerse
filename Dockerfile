FROM python:3.12-slim

WORKDIR /app

# PyNaCl for Ed25519 identity seats (same lib as Chamber)
RUN pip install --no-cache-dir pynacl

COPY identity/ ./identity/
COPY chat/ ./chat/
COPY spend/ ./spend/
COPY vendor/ ./vendor/

ENV PORT=8080
ENV RIDERXL_HOST=0.0.0.0
# Chamber envelope primitives are vendored (see vendor/chamber.py note);
# point the loader at the in-image copy instead of the lab VM path.
ENV RIDERXL_CHAMBER_SRC=/app/vendor/chamber.py
# Never run with the insecure permissions stub in production
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-m", "chat.server"]
