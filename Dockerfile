FROM python:3.11-slim

# ffmpeg ត្រូវការសម្រាប់ yt-dlp FFmpegExtractAudio postprocessor
# git ត្រូវការសម្រាប់ pip ដើម្បី clone yt-dlp ពី GitHub (requirements.txt)
# nodejs ត្រូវការជា JS runtime សម្រាប់ yt-dlp-ejs ដោះស្រាយ YouTube JS challenge (2026+)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg git nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "song_search_bot.py"]
