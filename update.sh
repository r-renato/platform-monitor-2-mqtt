
# stop the service
sudo systemctl stop p-monitor-2-mqtt.service

# get the latest version
sudo git pull

# reload the systemd configuration (in case it changed)
sudo systemctl daemon-reload

# restart the service with your new version
sudo systemctl start p-monitor-2-mqtt.service

# if you want, check status of the running script
systemctl status p-monitor-2-mqtt.service