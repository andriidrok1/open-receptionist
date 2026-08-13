# Self-hosted image: one image runs either the voice agent (default) or the
# owner dashboard (see docker-compose.yml). server/Dockerfile is a different
# thing — the Pipecat Cloud build.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so code edits do not re-download the world.
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps .env, *.db and recordings out of the image — an image
# gets pushed and shared, a customer database must not travel inside it.
COPY server/ ./

ENV PYTHONUNBUFFERED=1

EXPOSE 7860 8000

CMD ["python", "bot.py", "--host", "0.0.0.0"]
