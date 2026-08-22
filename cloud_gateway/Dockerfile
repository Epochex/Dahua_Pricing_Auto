FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app/cloud_gateway

ENV PORT=8080
CMD ["sh", "-c", "uvicorn cloud_gateway.app:app --host 0.0.0.0 --port ${PORT}"]
