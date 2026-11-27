FROM maven:3.9.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY pom.xml .
RUN mvn -q -DskipTests dependency:go-offline
COPY src src
RUN mvn -q -DskipTests package
FROM eclipse-temurin:25-jre
WORKDIR /app
COPY --from=build /src/target/inventory-service-0.1.0.jar app.jar
RUN groupadd -g 10001 app && useradd -u 10001 -g app -M app && chown -R app:app /app
USER 10001
ENTRYPOINT ["java","-Djava.io.tmpdir=/tmp","-jar","/app/app.jar"]
