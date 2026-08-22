FROM debian:bookworm-slim

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install essential storage inspection, benchmarking, and partition utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    smartmontools \
    sg3-utils \
    lsscsi \
    hdparm \
    fio \
    gdisk \
    parted \
    fwupd \
    jq \
    udev \
    util-linux \
    e2fsprogs \
    procps \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python requirements in virtual environment
COPY requirements.txt .
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/venv/bin:$PATH"

# Copy application source code
COPY app/ /app/app/
COPY entrypoint.sh /app/entrypoint.sh

# Create container-internal reports directory
RUN mkdir -p /app/reports

# Default exposed port
EXPOSE 7492

ENTRYPOINT ["/app/entrypoint.sh"]
