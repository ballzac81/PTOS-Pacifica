FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "signal_tracker:app"]
