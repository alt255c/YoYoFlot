FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md requirements.txt ./
COPY src ./src
COPY configs/scoring.yaml ./configs/scoring.yaml

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps .

ENV YOYO_HOST=0.0.0.0
ENV YOYO_PORT=8501
ENV YOYO_PRODUCT=/app/product
EXPOSE 8501

CMD ["python", "-m", "yoyoflot.product.web"]
