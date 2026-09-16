FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# tesseract читает машиночитаемую зону паспорта прямо здесь, на сервере:
# наружу скан не уходит, и вопрос 152-ФЗ с передачей ПДн третьему лицу
# не возникает вовсе. МЧЗ - только латиница и цифры, поэтому русских
# языковых данных не нужно, хватает eng из базового пакета.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Только список зависимостей: слой не пересобирается при правке кода.
# Единственный источник версий - requirements.txt, pyproject тянет его же.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY schema.sql ./schema.sql

# Не root: контейнер держит сканы паспортов, лишние права тут ни к чему
RUN useradd --system --uid 10001 --create-home bot \
    && mkdir -p /files/kyc \
    && chown -R bot:bot /srv /files
USER bot

CMD ["python", "-m", "app.main"]
