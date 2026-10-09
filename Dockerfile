FROM python:3.11-slim

WORKDIR /app

# System deps: ffmpeg (thumbnail + duration), curl, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Create volume mount point (Railway will mount here)
RUN mkdir -p /app/data && \
    mkdir -p /app/downloads /app/thumbs /app/github_downloads

# Non-root user (security)
RUN useradd -m -u 1000 bot && \
    chown -R bot:bot /app
USER bot

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RAILWAY_VOLUME_MOUNT_PATH=/app/data

CMD ["python", "bot.py"]
