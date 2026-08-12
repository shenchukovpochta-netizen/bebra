FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Только список зависимостей: слой не пересобирается при правке кода.
# Единственный источник версий - requirements.txt, pyproject тянет его же.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY schema.sql ./schema.sql
COPY fleet_schema.sql ./fleet_schema.sql

# Не root: контейнер держит сканы паспортов, лишние права тут ни к чему
RUN useradd --system --uid 10001 --create-home bot \
    && mkdir -p /files/kyc \
    && chown -R bot:bot /srv /files
USER bot

CMD ["python", "-m", "app.main"]
