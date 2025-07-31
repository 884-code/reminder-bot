# Python 3.11を使用
FROM python:3.11-slim

# 作業ディレクトリを /app に設定
WORKDIR /app

# app フォルダ内の内容をコンテナの /app にコピー
COPY app/ .

# 依存関係のインストール
RUN pip install -r requirements.txt

# ポートを開ける（Koyeb用）
EXPOSE 10000

# アプリの起動
CMD ["python", "mybot.py"]
