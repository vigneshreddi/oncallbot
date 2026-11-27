FROM python:3.15-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY investigator ./investigator
RUN addgroup -S -g 10002 investigator && adduser -S -u 10002 -G investigator investigator && chown -R investigator:investigator /app
USER 10002
CMD ["uvicorn", "investigator.api:app", "--host", "0.0.0.0", "--port", "8080"]
