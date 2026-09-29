FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
ENV OLC_INSTANCE=/data HOST=0.0.0.0 PORT=5210
EXPOSE 5210
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5210", "--timeout", "180", "app:app"]
