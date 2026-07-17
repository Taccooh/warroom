# ---- builder ----
# cffi, httptools and uvloop ship manylinux wheels for amd64/arm64 but not for
# arm/v7, so on that platform pip falls back to a source build. That needs a C
# toolchain (and libffi headers for cffi) — build it here, discard the
# toolchain in the runtime stage below. Rust is here too in case a future dep
# needs it on arm/v7 (current deps' wheels cover it as of writing).
FROM python:3.12-slim AS builder
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev curl \
    && rm -rf /var/lib/apt/lists/*
ENV CARGO_HOME=/root/.cargo PATH=/root/.cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /install /usr/local
COPY app ./app

# Data (SQLite DB, master.key, vapid.pem) comes in as a volume mount, not into the image.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
