#!/bin/bash

# Find and kill existing processes of app.py
PID=$(ps -aux | grep 'app.py' | grep -v 'grep' | awk '{print $2}')
if [ -n "$PID" ]; then
    echo "Killing existing app.py process(es) with PID(s): $PID"
    kill -9 $PID
else
    echo "No existing app.py process found."
fi

# Start the app in the background using nohup
echo "Starting app.py in the background..."
nohup /home/sysadmin/Ford/venv/bin/python /path/to/app.py > /path/to/logs/stdout.log 2>&1 &

echo "App started with PID $(ps -aux | grep 'app.py' | grep -v 'grep' | awk '{print $2}')"