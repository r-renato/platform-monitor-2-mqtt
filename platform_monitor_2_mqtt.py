from datetime import datetime, timedelta
from threading import Event, Thread
from tzlocal import get_localzone
import os
import sys
import ssl
from pydoc import locate
import json
import argparse
from configparser import ConfigParser
import logging
import logging.config
from time import sleep

import paho.mqtt.client as mqtt

from signal import signal, SIGPIPE, SIG_DFL, SIGINT
#signal(SIGPIPE, SIG_DFL)
	
script_version = "1.0.0"
script_name = 'platform-monitor-2-mqtt.py'
script_info = '{} v{}'.format(script_name, script_version)
project_name = 'platform-monitor-2-mqtt'
project_url = 'https://github.com/r-renato/platform-monitor-2-mqtt'

config = None
logger = None

monitor2MQTTCtrl = Event()

def sigintHandler(signum, frame):
    logger.info( "Daemon end.")
    monitor2MQTTCtrl.set()
    exit(0)

class Monitor2MQTT(Thread):
    _default_interval_in_minutes = 1
    _default_base_topic = 'home/nodes'
    _default_sensor_name = 'rpi-reporter'
    
    _lwt_topic = ''
    _lwt_online_val = 'online'
    _lwt_offline_val = 'offline'
    
    _mqtt_username = None
    _mqtt_password = None
    
    _mqtt_client_connected = False   
    _mqtt_client = None
    
    _interval_in_minutes = -1
    
    _modules = {}
    
    def __init__(self, event) -> None:
        Thread.__init__(self)
        self.stopped = event
        
        self._interval_in_minutes = int( config['Daemon'].getint('interval_in_minutes', self._default_interval_in_minutes) )
        if self._interval_in_minutes < self._default_interval_in_minutes:
            self._interval_in_minutes = self._default_interval_in_minutes
    
        logger.info('Monitor polling time {} minutes.'.format(self._interval_in_minutes) )
                
        def on_connect(client, userdata, flags, rc):
            #global mqtt_client_connected
            if rc == 0:
                logger.info('MQTT connection established. Base topic: ' + self._lwt_topic )
                # print_line('')  # blank line?!
                #_thread.start_new_thread(afterMQTTConnect, ())
                self._mqtt_client_connected = True
                logger.debug('on_connect() - mqtt_client_connected=[{}]'.format(self._mqtt_client_connected))
            else:
                logger.error('MQTT Connection error with result code {} - {}'.format(str(rc),mqtt.connack_string(rc)))
                # technically NOT useful but readying possible new shape...
                self._mqtt_client_connected = False
                logger.debug('on_connect() - mqtt_client_connected=[{}]'.format(self._mqtt_client_connected))
                # kill main thread
                os._exit(1)

        def on_publish(client, userdata, mid):
            logger.debug('on_publish() - MQTT Data successfully published.')
            pass
        
        self._mqtt_client = mqtt.Client()
        self._mqtt_client.on_connect = on_connect
        self._mqtt_client.on_publish = on_publish

        base_topic = config['MQTT topic'].get('base_topic', self._default_base_topic).lower()
        sensor_name = config['MQTT topic'].get('sensor_name', self._default_sensor_name).lower()
        self._lwt_topic = '{}/{}'.format(base_topic, sensor_name.lower())
        # logger.debug( 'MQTT base topic: ' + self._lwt_topic )
    
        self._mqtt_client.will_set(self._lwt_topic + '/availability', payload=self._lwt_offline_val, retain=True)
        
        if config['MQTT'].getboolean('tls', False):
            # According to the docs, setting PROTOCOL_SSLv23 "Selects the highest protocol version
            # that both the client and server support. Despite the name, this option can select
            # “TLS” protocols as well as “SSL”" - so this seems like a resonable default
            self._mqtt_client.tls_set(
                ca_certs=config['MQTT'].get('tls_ca_cert', None),
                keyfile=config['MQTT'].get('tls_keyfile', None),
                certfile=config['MQTT'].get('tls_certfile', None),
                tls_version=ssl.PROTOCOL_SSLv23
            )
    
        self._mqtt_username = os.environ.get("MQTT_USERNAME", config['MQTT'].get('username'))
        self._mqtt_password = os.environ.get("MQTT_PASSWORD", config['MQTT'].get('password', None))

        if self._mqtt_username:
            self._mqtt_client.username_pw_set(self._mqtt_username, self._mqtt_password)

    def connect(self) -> bool:
        try:
            logger.debug('connect() - Try connecting to {}:{}'
                         .format(
                            config['MQTT'].get('hostname', 'localhost'),
                            config['MQTT'].get('port', '1883')
                         ))
            self._mqtt_client.connect(
                os.environ.get('MQTT_HOSTNAME', config['MQTT'].get('hostname', 'localhost')),
                port=int(os.environ.get('MQTT_PORT', config['MQTT'].get('port', '1883'))),
                keepalive=config['MQTT'].getint('keepalive', 60)
            )
        except:
            logger.error('MQTT connection error. Please check your settings in the configuration file "monitor.ini"')
            # sys.exit(1)
            return False
        else:
            self._mqtt_client.loop_start()
            while self._mqtt_client_connected == False:  # wait in loop
                logger.debug('connect() - Wait on mqtt_client_connected=[{}]'.format(self._mqtt_client_connected))
                sleep(1.0)  # some slack to establish the connection   
        
            current_timestamp = datetime.now(get_localzone())
            self._mqtt_client.publish(self._lwt_topic + '/timestamp', payload=current_timestamp.isoformat(), retain=False)
            self._mqtt_client.publish(self._lwt_topic + '/availability', payload=self._lwt_online_val, retain=False)
            
        return True
    
    def _collect_data(self):
        localDict = {}
        for (each_key, each_val) in config.items('Modules'):
            split_val = [st.strip() for st in each_val.split(',')]
            
            # logger.debug( 'mods.' + split_val[0] + '.' + split_val[1])
            Cl = self._modules.get( each_key )
            
            if Cl is None:
                Cl = locate( 'mods.' + split_val[0] + '.' + split_val[1])(config)
                self._modules[ each_key ] = Cl
                
            Cl.collect()
            localDict[each_key] = Cl.getData()
            
        return( localDict )

    def execute(self):
        data = self._collect_data()
        jsonData = json.dumps(data, ensure_ascii=False).encode('utf8')
        
        self._mqtt_client.publish(self._lwt_topic + '/values', payload=jsonData.decode(), retain=True)
        
        current_timestamp = datetime.now(get_localzone())
        self._mqtt_client.publish(self._lwt_topic + '/timestamp', payload=current_timestamp.isoformat(), retain=True)
        self._mqtt_client.publish(self._lwt_topic + '/availability', payload=self._lwt_online_val, retain=False)
            
        if config['General'].get('save_json', False):
            filename = "./store/last.json"
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, "w") as f:
                f.write( jsonData.decode() ) 
                       
    def run(self):
        #
        while not self.stopped.wait( 60 * self._interval_in_minutes ):
            self.execute()

            
# Driver code
if __name__ == "__main__":
    
    # Argparse
    parser = argparse.ArgumentParser(description=project_name, epilog='For further details see: ' + project_url)
    
    parser.add_argument("-v", "--verbose",    help="increase output verbosity", action="store_true")
    parser.add_argument("-d", "--debug",      help="show debug output", action="store_true")
    parser.add_argument("-t", "--test",       help="only test, run once", action="store_true")
    parser.add_argument("-c", '--config_dir', help='set directory where monitor.ini is located', default=sys.path[0])
    parse_args = parser.parse_args()   
 
    config_dir = parse_args.config_dir
    opt_debug = parse_args.debug
    opt_verbose = parse_args.verbose
    opt_test = parse_args.test

    print(script_info + '\n')
    
    # Load configuration file
    config = ConfigParser(delimiters=('=', ), inline_comment_prefixes=('#'), interpolation=None)
    config.optionxform = str
    try:
        with open(os.path.join(config_dir, 'monitor.ini')) as config_file: config.read_file(config_file)
        logging.config.fileConfig( 'monitor.ini' )
    except IOError:
        print('No configuration file "monitor.ini"')
        sys.exit(1)    
    
    # create logger
    logger = logging.getLogger('platformMonitor')
 
    if opt_verbose:
        logger.info('Verbose enabled')
    if opt_debug:
        logger.info('Debug enabled')
        logger.setLevel(logging.DEBUG)
    if opt_test:
        logger.info('Test, run once')
        
    # 'application' code
    # logger.debug('debug message')
    # logger.info('info message')
    # logger.warning('warn message')
    # logger.error('error message')
    # logger.critical('critical message')    
    
    # ####### ####### ####### ####### ####### ####### #######
    # MQTT Setup
    # ####### ####### ####### ####### ####### ####### #######
    # mqttClient = MQTTClient()
    # mqttClient.connect()
    
    # collectedData = collect_data()
    
    # # Serializing json  
    # json_object = json.dumps( collectedData, indent = 4)
    # print(json_object)
    


    # now just hang in forever loop until script is stopped externally
    daemon_enabled = config['Daemon'].getboolean('enabled', True)

    try:
        monitor2MQTT = Monitor2MQTT(monitor2MQTTCtrl)
        
        if not monitor2MQTT.connect():
            exit(1)
        
        signal(SIGINT, sigintHandler)
        
        if not opt_test and daemon_enabled:
            logger.info( "Run as daemon")
            monitor2MQTT.start()
            while not opt_test and daemon_enabled:
                #  our INTERVAL timer does the work
                sleep(10000)
        else:
            logger.info( "Run once")
            monitor2MQTT.execute()
            logger.info( "End.")
            
    except (RuntimeError, TypeError, NameError):
        logger.error(NameError)
        sigintHandler(None, None)
        