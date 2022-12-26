import subprocess
import logging

class RPIDevice:
    _default_domain = ''
    
    _logger = None
    _modDict = {}
    
    def __init__(self, config) -> None:
        self._logger = logging.getLogger('platformMonitor')  
        
        self._modDict['info'] = self._getDeviceInfo()
            
    def collect(self):
        self._modDict = { 
            **self._modDict
            }
        self._modDict['memory'] = self._getMemoryInfo()
        self._modDict['cpu'] = self._getCPUInfo()
        self._modDict['storage'] = self._getStorageInfo()
        self._modDict['network'] = self._getNetworkInfo()
        self._modDict['temperature'] = self._getDeviceTemperature()
        
        for device in self._modDict['storage']:
            if "/" == device['mount_point']:
                self._modDict['info']['fs_total_gb'] = device['size_total_gb']
        
    def getData(self):
        return self._modDict
    
    def _getDataFromSubprocess(self, command):
        out = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _ = out.communicate()
        return( stdout.decode('utf-8').rstrip().lstrip() )

    def _getDeviceInfo(self):    
        localDict = {}
        
        key = 'board'
        command = '/bin/cat /proc/cpuinfo | /bin/egrep "Model" | cut -d: -f2'
        localDict[ key ] = self._getDataFromSubprocess( command ).replace(u'\u0000', '').lstrip()
        
        key = 'board_hardware'
        command = '/bin/cat /proc/cpuinfo | /bin/egrep "Hardware" | cut -d: -f2'
        localDict[ key ] = self._getDataFromSubprocess( command ).lstrip()
        
        key = 'board_revision'
        command = '/bin/cat /proc/cpuinfo | /bin/egrep "Revision" | cut -d: -f2'
        localDict[ key ] = self._getDataFromSubprocess( command ).lstrip()
                                
        key = 'board_serial'
        command = '/bin/cat /proc/cpuinfo | /bin/egrep "Serial" | cut -d: -f2'
        localDict[ key ] = self._getDataFromSubprocess( command ).lstrip()
        
        key = 'processor'
        command = '/bin/cat /proc/cpuinfo | /bin/egrep "model name" | head -1 | cut -d: -f2'
        localDict[ key ] = self._getDataFromSubprocess( command ).lstrip()        
        
        key = 'processor_cores'
        command = 'nproc --all'
        localDict[ key ] = int( self._getDataFromSubprocess( command ).lstrip() )   
        
        key = 'ram_total_mb'
        command = "free --mega | grep 'Mem:' | cut -d: -f2 | awk '{ print $1}'"
        localDict[ key ] = self.next_power_of_2(int( self._getDataFromSubprocess( command ).lstrip() ))*1024
        
        return( localDict )
    
    def next_power_of_2(self, size):
        size_as_nbr = int(size) - 1
        return 1 if size == 0 else (1 << size_as_nbr.bit_length()) / 1024

    def _getDeviceTemperature(self):
        localDict = {}
        
        key = 'cpu'
        command = "cat /sys/class/thermal/thermal_zone0/temp"
        localDict[ key ] = float(round(int(self._getDataFromSubprocess( command ).lstrip())/1000, 1))
        
        key = 'gpu'
        command = "vcgencmd measure_temp | grep  -o -E '[[:digit:]].*'"
        localDict[ key ] = float( self._getDataFromSubprocess( command ).lstrip().replace('\'C', '') )
        
        localDict['measurement'] = "°C"
        
        return( localDict )
    
    def _getStorageInfo(self):
        localDict = {}
    
        command = "/bin/df -m | /usr/bin/tail -n +2 | /bin/egrep -v 'tmpfs|boot'"
        response = self._getDataFromSubprocess( command ).split("\n")
        
        trimmedLines = []
        for currLine in response:
            trimmedLine = currLine.lstrip().rstrip()
            if len(trimmedLine) > 0:
                trimmedLines.append(trimmedLine)
        self._logger.debug('_getStorageInfo() trimmedLines=[{}]'.format(trimmedLines))
        
        #  RESPONSE EXAMPLES
        #
        #  Filesystem     1M-blocks  Used Available Use% Mounted on
        #  /dev/root          59998   9290     48208  17% /
        #  /dev/sda1         937872 177420    712743  20% /media/data
        # or
        #  /dev/root          59647  3328     53847   6% /
        #  /dev/sda1           3703    25      3472   1% /media/pi/SANDISK
        # or
        #  xxx.xxx.xxx.xxx:/srv/c2db7b94 200561 148655 41651 79% /

        # FAILING Case v1.4.0:
        # Here is the output of 'df -m'

        # Sys. de fichiers blocs de 1M Utilisé Disponible Uti% Monté sur
        # /dev/root 119774 41519 73358 37% /
        # devtmpfs 1570 0 1570 0% /dev
        # tmpfs 1699 0 1699 0% /dev/shm
        # tmpfs 1699 33 1667 2% /run
        # tmpfs 5 1 5 1% /run/lock
        # tmpfs 1699 0 1699 0% /sys/fs/cgroup
        # /dev/mmcblk0p1 253 55 198 22% /boot
        # tmpfs 340 0 340 0% /run/user/1000

        # FAILING Case v1.6.x (issue #61)
        # [[/bin/df: /mnt/sabrent: No such device or address',
        #   '/dev/root         119756  19503     95346  17% /',
        #   '/dev/sda1         953868 882178     71690  93% /media/usb0',
        #   '/dev/sdb1         976761  93684    883078  10% /media/pi/SSD']]
                         
        drivers = []
        for currLine in trimmedLines:
            deviceDict = {}
            if 'no such device' in currLine.lower():
                self._logger.debug('BAD LINE FORMAT, Skipped=[{}]'.format(currLine))
                continue
            lineParts = currLine.split()
            self._logger.debug('lineParts({})={}'.format(len(lineParts), lineParts))
            if len(lineParts) < 6:
                self._logger.debug('BAD LINE FORMAT, Skipped=[{}]'.format(lineParts))
                continue
            
            # tuple { total blocks, used%, mountPoint, device }
            #
            # new mech:
            #  Filesystem     1M-blocks  Used Available Use% Mounted on
            #     [0]           [1]       [2]     [3]    [4]   [5]
            #     [--]         [n-3]     [n-2]   [n-1]   [n]   [--]
            #  where  percent_field_index  is 'n'
            #

            # locate our % used field...
            for percent_field_index in range(len(lineParts) - 2, 1, -1):
                if '%' in lineParts[percent_field_index]:
                    break
            self._logger.debug('percent_field_index=[{}]'.format(percent_field_index))
            
            total_size_idx = percent_field_index - 3
            used_size_idx = percent_field_index - 2
            available_size_idx = percent_field_index - 1
            mount_idx = percent_field_index + 1

            # do we have a two part device name?
            device = lineParts[0]
            if total_size_idx != 1:
                device = '{} {}'.format(lineParts[0], lineParts[1])
            self._logger.debug('device=[{}]'.format(device))
            deviceDict['device'] = device
            
            # do we have a two part mount point?
            mount_point = lineParts[mount_idx]
            if len(lineParts) - 1 > mount_idx:
                mount_point = '{} {}'.format(
                    lineParts[mount_idx], lineParts[mount_idx + 1])
            self._logger.debug('mount_point=[{}]'.format(mount_point))
            deviceDict['mount_point'] = mount_point
            
            deviceDict['size_total_gb'] = int('{:.0f}'.format(self.next_power_of_2(lineParts[total_size_idx])))
            deviceDict['used_gb'] = int('{:.0f}'.format(self.next_power_of_2(lineParts[used_size_idx])))
            deviceDict['available_gb'] = int(deviceDict['size_total_gb'] - deviceDict['used_gb'])
            deviceDict['used_percentage'] = int(lineParts[percent_field_index].replace('%', ''))
            
            if deviceDict['size_total_gb'] > 0:
                drivers.append( deviceDict )
            
        return( drivers )
    
    def _getMemoryInfo(self):
        localDict = {}
        
        key = 'ram_total_kb'
        command = "free -k | grep 'Mem:' | cut -d: -f2 | awk '{ print $1}'"
        localDict[ key ] = int( self._getDataFromSubprocess( command ).lstrip() )  
        
        key = 'ram_used_kb'
        command = "free -k | grep 'Mem:' | cut -d: -f2 | awk '{ print $2}'"
        localDict[ key ] = int( self._getDataFromSubprocess( command ).lstrip() )  
        
        key = 'ram_free_kb'
        command = "free -k | grep 'Mem:' | cut -d: -f2 | awk '{ print $3}'"
        localDict[ key ] = int( self._getDataFromSubprocess( command ).lstrip() )  
        
        key = 'ram_available_kb'
        command = "free -k | grep 'Mem:' | cut -d: -f2 | awk '{ print $6}'"
        localDict[ key ] = int( self._getDataFromSubprocess( command ).lstrip() )  
        
        return( localDict )
    
    def _getNetworkInfo(self):
        localDict = {}
        
        command = "ip link show | /bin/egrep -v 'link' | /bin/egrep 'state UP' | /bin/egrep -v 'lo' | awk -F: '{ print $2}'"
        response = self._getDataFromSubprocess( command ).split("\n")
        
        trimmedLines = []
        for currLine in response:
            trimmedLine = currLine.lstrip().rstrip().lstrip() 
            if len(trimmedLine) > 0:
                trimmedLines.append(trimmedLine)
        self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(trimmedLines))
        
        for currLine in trimmedLines:
            netDict = {}
            
            command = "/sbin/ifconfig {} | /bin/egrep -w 'inet'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                netDict['ip'] = lineParts[ 1 ]
                netDict['mask'] = lineParts[ 3 ]
                netDict['broadcast'] = lineParts[ 5 ]

            command = "/sbin/ifconfig {} | /bin/egrep -w 'inet6'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                netDict['ip6'] = lineParts[ 1 ]
            
            command = "/sbin/ifconfig {} | /bin/egrep -w 'ether'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                netDict['mac'] = lineParts[ 1 ]
                
            rxDict = {}
            command = "/sbin/ifconfig {} | /bin/egrep -w 'RX packets'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                rxDict['packets'] = int(lineParts[ 2 ])
                rxDict['bytes'] = int(lineParts[ 4 ])
            
            command = "/sbin/ifconfig {} | /bin/egrep -w 'RX errors'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                rxDict['errors'] = int(lineParts[ 2 ])
                rxDict['dropped'] = int(lineParts[ 4 ])
                rxDict['overruns'] = int(lineParts[ 6 ])
                rxDict['frame'] = int(lineParts[ 8 ])
                
            netDict['rx'] = rxDict            
            
            txDict = {}
            command = "/sbin/ifconfig {} | /bin/egrep -w 'TX packets'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                txDict['packets'] = int(lineParts[ 2 ])
                txDict['bytes'] = int(lineParts[ 4 ])
                
            command = "/sbin/ifconfig {} | /bin/egrep -w 'TX errors'".format(currLine)
            response = self._getDataFromSubprocess( command ).rstrip().lstrip()        
            self._logger.debug('_getNetworkInfo() trimmedLines=[{}]'.format(response))
            if len(response) > 0:
                lineParts = response.split()
                txDict['errors'] = int(lineParts[ 2 ])
                txDict['dropped'] = int(lineParts[ 4 ])
                txDict['overruns'] = int(lineParts[ 6 ])
                txDict['carrier'] = int(lineParts[ 8 ])
                txDict['collisions'] = int(lineParts[ 10 ])
                
            netDict['tx'] = txDict
            
            localDict[currLine] = netDict
            
        return( localDict )

    def _getCPUInfo(self):
        localDict = {}

        command = "grep 'cpu ' /proc/stat"
        response = self._getDataFromSubprocess( command )
        lineParts = response.split()

        localDict['normal_processes_user_mode'] = int(lineParts[ 0 ])
        localDict['nice_processes_user_mode'] = int(lineParts[ 1 ])
        localDict['system_processes_kernel_mode'] = int(lineParts[ 2 ])
        localDict['idle_processes'] = int(lineParts[ 3 ])
        localDict['iowait_processes'] = int(lineParts[ 4 ])
        localDict['irq_processes'] = int(lineParts[ 5 ])
        localDict['softirq_processes'] = int(lineParts[ 6 ])
        localDict['steal_processes'] = int(lineParts[ 7 ])
        localDict['guest_processes'] = int(lineParts[ 8 ])
        localDict['guest_nice_processes'] = int(lineParts[ 9 ])
        
        total: int = localDict['normal_processes_user_mode'] 
        + localDict['nice_processes_user_mode']
        + localDict['system_processes_kernel_mode']
        + localDict['idle_processes']
        + localDict['iowait_processes']
        + localDict['irq_processes']
        + localDict['softirq_processes']
        
        localDict['average_idle_percentage'] = round((localDict['idle_processes'] * 100 ) / total, 1)
        
        return( localDict )