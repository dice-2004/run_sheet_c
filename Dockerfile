# 画面 (web): コードは実行しないので、コンパイラは入れない
FROM python:3.12-slim AS web
COPY docker/requirements-web.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN useradd -m runner \
    && mkdir /work && chown runner /work   # 共有ボリュームの初期所有者になる
WORKDIR /app
COPY app.py diffview.py scoring.py ./
USER runner
ENTRYPOINT ["python", "/app/app.py"]


# 実行 (worker): 学生の環境 (c1byod の MSYS2: gcc 11.3.0) に合わせて、公式の gcc 11.3.0 で動かす
FROM gcc:11.3.0 AS worker
COPY docker/apt-worker.txt /tmp/apt.txt
RUN apt-get update \
    && grep -vE '^\s*(#|$)' /tmp/apt.txt | xargs apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /tmp/apt.txt
# 回答コードを実行するため、権限のない専用ユーザーで動かす
RUN useradd -m runner \
    && mkdir /work && chown runner /work
WORKDIR /app
COPY run_sheet_c.py worker.py diffview.py ./
USER runner
ENTRYPOINT ["python3", "/app/run_sheet_c.py"]
