# Image minimale : le serveur n'utilise que la stdlib Python.
FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PORT=8741 HOST=0.0.0.0
EXPOSE 8741
CMD ["python3", "server.py"]
