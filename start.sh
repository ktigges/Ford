#!/bin/bash

# Optional flag to auto-start the poller
ENABLE_POLLER=false
if [[ "$1" == "--enable-poller" ]] || [[ "$1" == "--start-poller" ]]; then
    ENABLE_POLLER=true
    echo "Poller will be started after app is ready."
fi

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

APP_PID=$(ps -aux | grep 'app.py' | grep -v 'grep' | awk '{print $2}')
echo "App started with PID $APP_PID"

# Wait for the app to be ready on port 5000 and optionally start poller
if [ "$ENABLE_POLLER" = true ]; then
    echo "Waiting for app to be ready..."
    for i in {1..30}; do
        if nc -z localhost 5000 2>/dev/null; then
            echo "App is ready. Starting poller..."
            sleep 1
            curl -s -X POST http://localhost:5000/poller -d "action=start" > /dev/null 2>&1
            if [ $? -eq 0 ]; then
                echo "Poller started successfully."
            else
                echo "Warning: Could not start poller via API."
            fi
            break
        fi
        sleep 1
    done
fi