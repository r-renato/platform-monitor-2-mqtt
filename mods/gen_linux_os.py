import subprocess
import logging

class GenericLinuxOS:
    _default_domain = ''
    
    _logger = None
    _modDict = {}
    
    _fallback_domain = None
    
    def __init__(self, config) -> None:
        self._logger = logging.getLogger('platformMonitor')  
        
        # default domain when hostname -f doesn't return it
        self._fallback_domain = config['General'].get('fallback_domain', self._default_domain).lower()
        self._modDict = {**self._modDict, **self._getLinuxOSInfo()}
            
    def collect(self):
        self._modDict = { 
            **self._modDict,
            **self._getHostnames(),
            **self._getUptime()
            }
        
    def getData(self):
        return self._modDict
    
    def _getDataFromSubprocess(self, command):
        out = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _ = out.communicate()
        return( stdout.decode('utf-8').rstrip() )
    
    def _getHostnames(self):
        localDict = {}
        out = subprocess.Popen("/bin/hostname -f",
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
        stdout, _ = out.communicate()
        fqdn_raw = stdout.decode('utf-8').rstrip()
        localDict['fqdn_raw'] = fqdn_raw
        # print( 'fqdn_raw=[{}]'.format(fqdn_raw) )
        # print_line('fqdn_raw=[{}]'.format(fqdn_raw), debug=True)
        rpi_hostname = fqdn_raw
        self._logger.debug('fqdn_raw: ' + fqdn_raw)
        if '.' in fqdn_raw:
            # have good fqdn
            nameParts = fqdn_raw.split('.')
            rpi_fqdn = fqdn_raw
            rpi_hostname = nameParts[0]
        else:
            # missing domain, if we have a fallback apply it
            if len(self._fallback_domain) > 0:
                rpi_fqdn = '{}.{}'.format(fqdn_raw, self._fallback_domain)
            else:
                rpi_fqdn = rpi_hostname

        localDict['rpi_fqdn'] = rpi_fqdn
        localDict['rpi_hostname'] = rpi_hostname
        # print_line('rpi_fqdn=[{}]'.format(rpi_fqdn), debug=True)
        # print_line('rpi_hostname=[{}]'.format(rpi_hostname), debug=True)        
        return( localDict )
        
    def _getLinuxOSInfo(self):    
        localDict = {}
        
        # Get
        key = 'linux_distribution_name'
        command = '/bin/cat /etc/os-release | /bin/egrep "^NAME=" | cut -d\'"\' -f 2'
        localDict[ key ] = self._getDataFromSubprocess( command )
        
        key = 'linux_distribution_derived'
        command = '/bin/cat /etc/os-release | /bin/egrep "ID_LIKE" | cut -d= -f2'
        localDict[ key ] = self._getDataFromSubprocess( command )
        
        key = 'linux_distribution_release'
        command = "/bin/cat /etc/apt/sources.list | /bin/egrep -v '#' | /usr/bin/awk '{ print $3 }' | /bin/sed -e 's/-/ /g' | /usr/bin/cut -f1 -d' ' | /bin/grep . | /usr/bin/sort -u"
        localDict[ key ] = self._getDataFromSubprocess( command )
                
        key = 'linux_distribution_release_version'
        command = "/bin/uname -r"
        localDict[ key ] = self._getDataFromSubprocess( command )
                        
        return( localDict )
    
    def _getUptime(self):    
        localDict = {}
        
        key = 'uptime'
        command = "/usr/bin/uptime -p"
        localDict[ key ] = self._getDataFromSubprocess( command )     
        
        key = 'uptime_since'
        command = "/usr/bin/uptime -s"
        localDict[ key ] = self._getDataFromSubprocess( command )   
        
        key = 'uptime_seconds'
        command = "/bin/cat /proc/uptime | awk '{printf \"%0.f\", $1}'"
        localDict[ key ] = int( self._getDataFromSubprocess( command ) )
                
        return( localDict )  
        
    def _getUptimeo(self):
        localDict = {}
        out = subprocess.Popen("/usr/bin/uptime",
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
        stdout, _ = out.communicate()
        rpi_uptime_raw = stdout.decode('utf-8').rstrip().lstrip()
        localDict['rpi_uptime_raw'] = rpi_uptime_raw
        # print_line('rpi_uptime_raw=[{}]'.format(rpi_uptime_raw), debug=True)
        basicParts = rpi_uptime_raw.split()
        timeStamp = basicParts[0]
        lineParts = rpi_uptime_raw.split(',')
        if('user' in lineParts[1]):
            rpi_uptime_raw = lineParts[0]
        else:
            rpi_uptime_raw = '{}, {}'.format(lineParts[0], lineParts[1])
        rpi_uptime = rpi_uptime_raw.replace(
            timeStamp, '').lstrip().replace('up ', '')
        localDict['rpi_uptime'] = rpi_uptime
        # print_line('rpi_uptime=[{}]'.format(rpi_uptime), debug=True) 
        return( localDict )   
    
    