"""
Proxmox API Manager
Handles LXC container operations on Proxmox cluster
"""
import logging
import requests
import urllib3
from typing import Dict, Optional, List

# Disable SSL warnings if verify_ssl is False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class ProxmoxManager:
    """Manages Proxmox VE API interactions for LXC containers"""
    
    def __init__(self, host: str, user: str, password: str, verify_ssl: bool = False):
        """
        Initialize Proxmox API manager
        
        Args:
            host: Proxmox host URL (e.g., 'https://proxmox.example.com:8006')
            user: Proxmox username (e.g., 'root@pam')
            password: Proxmox password
            verify_ssl: Whether to verify SSL certificates
        """
        self.host = host.rstrip('/')
        self.user = user
        self.password = password
        self.verify_ssl = verify_ssl
        self.ticket = None
        self.csrf_token = None
        self._authenticate()
    
    def _authenticate(self):
        """Authenticate with Proxmox API and get ticket"""
        url = f"{self.host}/api2/json/access/ticket"
        data = {
            'username': self.user,
            'password': self.password
        }
        
        try:
            response = requests.post(url, data=data, verify=self.verify_ssl)
            response.raise_for_status()
            result = response.json()['data']
            self.ticket = result['ticket']
            self.csrf_token = result['CSRFPreventionToken']
            logger.info("Successfully authenticated with Proxmox API")
        except Exception as e:
            logger.error(f"Proxmox authentication failed: {str(e)}")
            raise
    
    def _get_headers(self):
        """Get request headers with authentication"""
        return {
            'CSRFPreventionToken': self.csrf_token
        }
    
    def _get_cookies(self):
        """Get request cookies with authentication"""
        return {
            'PVEAuthCookie': self.ticket
        }
    
    def _request(self, method: str, endpoint: str, data: Optional[Dict] = None):
        """Make authenticated request to Proxmox API"""
        url = f"{self.host}/api2/json{endpoint}"
        
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self._get_headers(),
                cookies=self._get_cookies(),
                data=data,
                verify=self.verify_ssl
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                # Re-authenticate and retry
                logger.info("Token expired, re-authenticating...")
                self._authenticate()
                return self._request(method, endpoint, data)
            raise
    
    def get_nodes(self) -> List[Dict]:
        """Get list of Proxmox nodes"""
        result = self._request('GET', '/nodes')
        return result['data']
    
    def get_next_vmid(self) -> int:
        """Get next available VMID"""
        result = self._request('GET', '/cluster/nextid')
        return int(result['data'])
    
    def create_lxc_container(
        self,
        node: str,
        vmid: int,
        hostname: str,
        ostemplate: str,
        storage: str = 'local-lvm',
        memory: int = 4096,
        swap: int = 512,
        cores: int = 2,
        disk_size: int = 20,
        network: Optional[Dict] = None,
        password: Optional[str] = None
    ) -> Dict:
        """
        Create a new LXC container
        
        Args:
            node: Target Proxmox node
            vmid: Container ID
            hostname: Container hostname
            ostemplate: OS template (e.g., 'local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst')
            storage: Storage for container
            memory: RAM in MB
            swap: Swap in MB
            cores: CPU cores
            disk_size: Root disk size in GB
            network: Network configuration dict
            password: Root password
        """
        endpoint = f'/nodes/{node}/lxc'
        
        if network is None:
            network = {
                'name': 'eth0',
                'bridge': 'vmbr0',
                'firewall': 1,
                'ip': 'dhcp'
            }
        
        data = {
            'vmid': vmid,
            'hostname': hostname,
            'ostemplate': ostemplate,
            'storage': storage,
            'memory': memory,
            'swap': swap,
            'cores': cores,
            'rootfs': f"{storage}:{disk_size}",
            'net0': f"name={network['name']},bridge={network['bridge']},firewall={network['firewall']},ip={network['ip']}",
            'unprivileged': 1,
            'start': 1
        }
        
        if password:
            data['password'] = password
        
        logger.info(f"Creating LXC container {vmid} on node {node}")
        result = self._request('POST', endpoint, data)
        return result
    
    def get_container_status(self, node: str, vmid: int) -> str:
        """Get container status"""
        endpoint = f'/nodes/{node}/lxc/{vmid}/status/current'
        result = self._request('GET', endpoint)
        return result['data']['status']
    
    def get_container_config(self, node: str, vmid: int) -> Dict:
        """Get container configuration"""
        endpoint = f'/nodes/{node}/lxc/{vmid}/config'
        result = self._request('GET', endpoint)
        return result['data']
    
    def start_container(self, node: str, vmid: int):
        """Start a container"""
        endpoint = f'/nodes/{node}/lxc/{vmid}/status/start'
        logger.info(f"Starting container {vmid} on node {node}")
        return self._request('POST', endpoint)
    
    def stop_container(self, node: str, vmid: int):
        """Stop a container"""
        endpoint = f'/nodes/{node}/lxc/{vmid}/status/stop'
        logger.info(f"Stopping container {vmid} on node {node}")
        return self._request('POST', endpoint)
    
    def shutdown_container(self, node: str, vmid: int, timeout: int = 60):
        """Gracefully shutdown a container"""
        endpoint = f'/nodes/{node}/lxc/{vmid}/status/shutdown'
        data = {'timeout': timeout}
        logger.info(f"Shutting down container {vmid} on node {node}")
        return self._request('POST', endpoint, data)
    
    def delete_container(self, node: str, vmid: int, purge: bool = True):
        """Delete a container"""
        # Stop container first
        try:
            status = self.get_container_status(node, vmid)
            if status == 'running':
                self.stop_container(node, vmid)
        except Exception as e:
            logger.warning(f"Error stopping container before deletion: {str(e)}")
        
        endpoint = f'/nodes/{node}/lxc/{vmid}'
        params = {'purge': int(purge)}
        logger.info(f"Deleting container {vmid} from node {node}")
        return self._request('DELETE', endpoint, params)
    
    def get_container_ip(self, node: str, vmid: int) -> Optional[str]:
        """Get container IP address"""
        try:
            endpoint = f'/nodes/{node}/lxc/{vmid}/interfaces'
            result = self._request('GET', endpoint)
            
            # Look for eth0 interface
            for interface in result['data']:
                if interface.get('name') == 'eth0':
                    if 'inet' in interface:
                        return interface['inet'].split('/')[0]
            return None
        except Exception as e:
            logger.error(f"Error getting container IP: {str(e)}")
            return None
    
    def execute_command(self, node: str, vmid: int, command: str) -> Dict:
        """Execute command in container"""
        endpoint = f'/nodes/{node}/lxc/{vmid}/exec'
        data = {'command': command}
        return self._request('POST', endpoint, data)
    
    def list_containers(self, node: str) -> List[Dict]:
        """List all containers on a node"""
        endpoint = f'/nodes/{node}/lxc'
        result = self._request('GET', endpoint)
        return result['data']
    
    def get_cluster_resources(self) -> List[Dict]:
        """Get cluster resources"""
        endpoint = '/cluster/resources'
        result = self._request('GET', endpoint)
        return result['data']
    
    def find_least_loaded_node(self) -> str:
        """Find the node with least load"""
        nodes = self.get_nodes()
        
        # Find node with lowest CPU usage
        least_loaded = min(nodes, key=lambda n: n.get('cpu', 1.0))
        return least_loaded['node']
