FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-impress fonts-dejavu-core fonts-liberation && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /tmp/core-requirements.txt
COPY webapp/backend/requirements.txt /tmp/web-requirements.txt
RUN pip install --no-cache-dir -r /tmp/core-requirements.txt -r /tmp/web-requirements.txt
COPY . /app
ENV PYTHONPATH=/app
CMD ["uvicorn", "webapp.backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
