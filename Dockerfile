FROM python:3.10-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip install playwright requests python-dotenv

# Install Chromium and system dependencies
RUN playwright install chromium && playwright install-deps

WORKDIR /app

# Copy the worker script
COPY publicapi.py /app/worker.py

# Run the worker (continuous loop)
CMD ["python", "-u", "worker.py"]
