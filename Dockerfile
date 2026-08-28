FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install fastapi uvicorn playwright requests python-dotenv
RUN playwright install chromium && playwright install-deps

WORKDIR /app
COPY publicapi.py /app/worker.py   # or main.py

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "3000"]
