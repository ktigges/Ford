nohup python app.py > logs/stdout.log 2>&1 &
echo $!  # prints the PID — save this to stop later