FROM mokioclaw-ci:38b0d22a8dff4e22
RUN apt-get update && apt-get install -y --no-install-recommends make && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.19
RUN uv python install 3.13.9
