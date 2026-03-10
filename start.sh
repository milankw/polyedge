#!/bin/bash
set -e

echo "Starting PolyEdge Prediction Engine..."

# Start API server in background
cd /app/backend
python api_server.py &
API_PID=$!
echo "API server started (PID: $API_PID)"

# Wait for API to be ready
sleep 2

# Start frontend
echo "Starting frontend on port 80..."
serve /app/frontend -l tcp://0.0.0.0:80 --no-clipboard --single &
WEB_PID=$!
echo "Frontend started (PID: $WEB_PID)"

echo ""
echo "==================================="
echo "  PolyEdge is running!"
echo "  Dashboard: http://localhost"
echo "  API:       http://localhost:8000"
echo "==================================="
echo ""

# Wait for either process to exit
wait -n $API_PID $WEB_PID
exit $?
