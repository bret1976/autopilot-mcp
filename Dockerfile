FROM python:3.12.16-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip fonts-dejavu-core \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import curl_cffi; print('curl_cffi', curl_cffi.__version__)" \
    && yt-dlp --version
# Footage engine (ORIGINALS_ENGINE=footage): bake the free Apache-2.0 Kokoro TTS model into the image and prove
# synthesis works at build time, so a buyer's video never needs a Gemini key or a runtime model download.
ENV KOKORO_MODEL=/opt/kokoro/kokoro-v1.0.int8.onnx \
    KOKORO_VOICES=/opt/kokoro/voices-v1.0.bin
RUN mkdir -p /opt/kokoro \
    && curl -fsSL -o /opt/kokoro/kokoro-v1.0.int8.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx \
    && curl -fsSL -o /opt/kokoro/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin \
    && python -c "import os; from kokoro_onnx import Kokoro; k = Kokoro(os.environ['KOKORO_MODEL'], os.environ['KOKORO_VOICES']); a, sr = k.create('Footage mode voice check.', voice='am_michael', speed=1.0, lang='en-us'); assert sr == 24000 and len(a) > 12000, len(a); print('KOKORO_BUILD_OK', len(a) / sr, 's')"
COPY . .

ENV PORT=8080
ENV PATH="/usr/local/bin:${PATH}"
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
