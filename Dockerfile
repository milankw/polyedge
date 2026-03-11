FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY polymarket_100_wallets.csv .
COPY start.sh .
RUN chmod +x start.sh
EXPOSE 8892
CMD ["./start.sh"]
