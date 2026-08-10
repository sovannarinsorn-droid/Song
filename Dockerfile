FROM python:3.11-slim

# ffmpeg ត្រូវការសម្រាប់ yt-dlp FFmpegExtractAudio postprocessor
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "song_search_bot.py"]
