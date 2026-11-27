FROM python:3.12-slim
ARG SOURCE_MANIFEST_B64=""
ENV SOURCE_MANIFEST_B64=${SOURCE_MANIFEST_B64}
WORKDIR /app
COPY indexer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY indexer/index_code.py /app/index_code.py
COPY services/order-service/src /src/services/order-service/src
COPY services/order-service/pom.xml /src/services/order-service/pom.xml
COPY services/inventory-service/src /src/services/inventory-service/src
COPY services/inventory-service/pom.xml /src/services/inventory-service/pom.xml
COPY services/payment-service/src /src/services/payment-service/src
COPY services/payment-service/pom.xml /src/services/payment-service/pom.xml
RUN groupadd --gid 10003 indexer && useradd --uid 10003 --gid indexer --no-create-home indexer
USER 10003
ENV SOURCE_ROOT=/src
CMD ["python", "/app/index_code.py"]
