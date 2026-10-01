FROM python:3.14-slim

# git is required by the control plane (baseline capture and verification).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package from source (see .dockerignore for what is excluded).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd -m appuser
USER appuser
WORKDIR /home/appuser

ENTRYPOINT ["howlplane"]
CMD ["--help"]
