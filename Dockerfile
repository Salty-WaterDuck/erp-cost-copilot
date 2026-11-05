FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CC_LLM_PROVIDER=offline

WORKDIR /app

COPY service/requirements.txt ./service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt

COPY costcontrol ./costcontrol
COPY data ./data
COPY config_templates ./config_templates
COPY docs ./docs
COPY eval ./eval
COPY README.md ./
COPY service ./service

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8000"]
