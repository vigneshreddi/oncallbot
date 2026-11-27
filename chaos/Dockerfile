FROM python:3.15-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY runner/chaos_runner.py .
COPY faults.yaml .
ENV FAULTS_PATH=/app/faults.yaml
RUN addgroup -S -g 10004 chaos && adduser -S -u 10004 -G chaos chaos
USER 10004
CMD ["python", "chaos_runner.py"]
