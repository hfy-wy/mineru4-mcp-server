FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV MINERU_UPLOAD_DIR=/data/uploads \
    MINERU_OUTPUT_DIR=/data/output
VOLUME ["/data"]

EXPOSE 7000
ENTRYPOINT ["mineru-selfhosted-mcp"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "7000"]
