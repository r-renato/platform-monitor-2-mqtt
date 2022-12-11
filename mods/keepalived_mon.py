import subprocess
import logging
import re 

class KeepalivedMon:
    _logger = None
    _modDict = {}
    
    _keepalived_cmd = None
    
    def __init__(self, config) -> None:
        self._logger = logging.getLogger('platformMonitor')  
        
        self._keepalived_cmd = self._retrieveKeepalivedCommand()
        if len(self._keepalived_cmd) > 0:
            self._modDict['info'] = self._getKeepalivedInfo()
    
    def getData(self):
        return self._modDict    
    
    def _getDataFromSubprocess(self, command):
        # self._logger.info( command )
        out = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _ = out.communicate()
        return( stdout.decode('utf-8').rstrip().lstrip() )
    
    def _retrieveKeepalivedCommand(self):
        command = 'which keepalived'
        return( self._getDataFromSubprocess( command ) )
    
    def collect(self):
        self._modDict = { 
            **self._modDict
            }
        self._modDict['core'] = self._getKeepalivedData()
        
    def _getKeepalivedInfo(self):
        localDict = {}
        
        key = 'keepalived_version'
        command = self._docker_cmd + " --version"
        response = self._getDataFromSubprocess( command ).split("\n")   
        splittedLine = response[0].lstrip().rstrip().split() 
        
        localDict['keepalived_version'] = splittedLine[1]
        localDict['keepalived_date'] = re.sub(r"[\([{})\]]", "", splittedLine[2])
        
        return( localDict )    
    
    def _getKeepalivedData(self):
        localDict = {}    
    
        key = 'service'
        command = "systemctl status keepalived.service | grep 'Active' | awk '{ print $2}'"
        localDict[key] = self._getDataFromSubprocess( command )
        
        return( localDict )  