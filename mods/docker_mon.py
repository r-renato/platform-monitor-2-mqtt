import subprocess
import logging

class DocketMon:
    _logger = None
    _modDict = {}
    
    _docker_cmd = None
    
    def __init__(self, config) -> None:
        self._logger = logging.getLogger('platformMonitor')  
        
        self._docker_cmd = self._retrieveDockerCommand()
        if len(self._docker_cmd) > 0:
            self._modDict['info'] = self._getDockerInfo()
    
    def getData(self):
        return self._modDict    
    
    def _getDataFromSubprocess(self, command):
        # self._logger.info( command )
        out = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _ = out.communicate()
        return( stdout.decode('utf-8').rstrip().lstrip() )
    
    def _retrieveDockerCommand(self):
        command = 'which docker'
        return( self._getDataFromSubprocess( command ) )
        
    def collect(self):
        self._modDict = { 
            **self._modDict,
            **self._getImagesAndContainersData()
            }
        
    def _getDockerInfo(self):
        localDict = {}
        
        key = 'docker_version'
        command = self._docker_cmd + " --version | awk '{print $3}' | cut -d ',' -f 1"
        localDict[ key ] = self._getDataFromSubprocess( command )        
        
        key = 'docker_build'
        command = self._docker_cmd + " --version | awk '{print $5}'"
        localDict[ key ] = self._getDataFromSubprocess( command )  
        
        return( localDict )
        
    def _getImagesAndContainersData(self):
        localDict = {}
        
        key = 'docker_version'
        command = "sudo " + self._docker_cmd + " images -a | tail -n +2"
        imagesResponse = self._getDataFromSubprocess( command ).split("\n")        
        
        for currLine in imagesResponse:
            rowDict = {}
            trimmedLine = currLine.lstrip().rstrip()
            if len(trimmedLine) == 0:
                continue
            
            lineParts = trimmedLine.split()
            repository = lineParts[0]
            rowDict['image_tag'] = lineParts[1]
            rowDict['image_id'] = lineParts[2]
            rowDict['image_size'] = lineParts[len(lineParts) - 1]
            rowDict['container'] = 'Down'
            
            command = self._docker_cmd + " container ls -a | tail -n +2 | grep '{}'".format( repository )
            containerResponse = self._getDataFromSubprocess( command )
            trimmedLine = containerResponse.lstrip().rstrip()
            if len(trimmedLine) == 0:
                localDict[repository] = rowDict
                continue
            lineParts = trimmedLine.split()
            rowDict['container_id'] = lineParts[0]
            
            command = self._docker_cmd + " container stats --no-stream | tail -n +2 | grep '{}'".format( lineParts[0] )
            containerResponse = self._getDataFromSubprocess( command )
            trimmedLine = containerResponse.lstrip().rstrip()
            if len(trimmedLine) == 0:
                localDict[repository] = rowDict
                continue
            lineParts = trimmedLine.split()
            rowDict['container'] = 'Up'
            rowDict['container_cpu'] = lineParts[2]
            rowDict['container_mem_used_limit'] = lineParts[5]
            rowDict['container_mem_used'] = lineParts[3]
            rowDict['container_mem_used_percentage'] = lineParts[6]
            rowDict['container_net_in'] = lineParts[7]
            rowDict['container_net_out'] = lineParts[9]            
            rowDict['container_block_in'] = lineParts[10]
            rowDict['container_block_out'] = lineParts[12]   
            
            localDict[repository] = rowDict
            
        return( localDict )
        
        