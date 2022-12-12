# platform-monitor-2-mqtt





sudo git clone https://github.com/r-renato/platform-monitor-2-mqtt.git /opt/platform-monitor-2-mqtt

cd /opt/platform-monitor-2-mqtt
sudo pip3 install -r requirements.txt


sudo cp /opt/platform-monitor-2-mqtt/monitor.{ini.dist,ini}


sudo ln -s /opt/platform-monitor-2-mqtt/p-monitor-2-mqtt.service /etc/systemd/system/p-monitor-2-mqtt.service

sudo systemctl daemon-reload
sudo systemctl enable p-monitor-2-mqtt.service

