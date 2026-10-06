FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# 私鑰與資料庫用 volume 掛載，不打包進 image
ENV LICENSE_PRIVATE_KEY=/srv/secrets/license_private.pem LICENSE_DB=/srv/data/licenses.db
EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
