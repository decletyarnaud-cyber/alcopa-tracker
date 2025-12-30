#!/bin/bash
cd "$(dirname "$0")"

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -q flask requests beautifulsoup4 lxml
fi

echo "Starting Alcopa Tracker..."
echo "Open http://localhost:5000 in your browser"
python app.py
