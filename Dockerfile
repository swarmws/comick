FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY comick.py .

EXPOSE 4775

# Run with Gunicorn + Uvicorn workers, stdout logs go to console
ENTRYPOINT ["gunicorn", "comick:app", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:4775", "--log-level", "info", "--access-logfile", "-", "--error-logfile", "-"]
