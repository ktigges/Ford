#!/bin/bash

# Kill any running instance of app.py
PID=$(ps -aux | grep 'app.py' | grep -v 'grep' | awk '{print $2}')
if [ -n "$PID" ]; then
    echo "Killing existing app.py process(es) with PID(s): $PID"
    kill -9 $PID
else
    echo "No existing app.py process found."
fi

# Wait a second or two to ensure the process is fully terminated before starting a new one
sleep 2

# Start the app in the background using nohup
echo "Starting app.py in the background..."
nohup /home/sysadmin/Ford/venv/bin/python /home/sysadmin/Ford/app.py > /home/sysadmin/Ford/logs/stdout.log 2>&1 &

echo "App started with PID $(ps -aux | grep 'app.py' | grep -v 'grep' | awk '{print $2}')"