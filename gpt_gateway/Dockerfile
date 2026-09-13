FROM python:3.11-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY gpt_gateway/requirements.txt /app/gpt_gateway/requirements.txt
RUN python -m pip install -r requirements.txt -r gpt_gateway/requirements.txt

COPY remotion-composer/package*.json /app/remotion-composer/
RUN cd /app/remotion-composer && npm install

COPY . /app

EXPOSE 8000
CMD ["uvicorn", "gpt_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
