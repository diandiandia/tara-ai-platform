FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for build packages and sqlite3
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy backend codebase
COPY . .

# Ensure the entrypoint script exists and is executable
RUN chmod +x /app/docker-entrypoint.sh

# Expose FastAPI port
EXPOSE 8000

# Run FastAPI app by default
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]