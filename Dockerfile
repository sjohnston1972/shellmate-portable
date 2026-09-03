FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY run.py .

RUN mkdir -p /app/ShellMate-Data

EXPOSE 8765

# Through run.py, not uvicorn directly (#506). run.py is where the
# bind-address refusal lives: bound wider than loopback with no
# SHELLMATE_AUTH_TOKEN, it exits rather than serving SSH sessions and saved
# credentials to the whole network. Running backend.app:app skipped that
# check, and ignored SHELLMATE_HOST and SHELLMATE_PORT too. --no-window
# serves and opens nothing, which is all a container can do.
ENV SHELLMATE_HOST=0.0.0.0     SHELLMATE_PORT=8765

CMD ["python", "run.py", "--no-window"]
