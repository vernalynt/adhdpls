FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py immich.py gcal.py ./
COPY static ./static
ENV DATA_DIR=/data
EXPOSE 5055
CMD ["gunicorn", "-b", "0.0.0.0:5055", "-w", "1", "app:app"]
