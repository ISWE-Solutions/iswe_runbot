"""
Ansible Runner
Executes Ansible playbooks for container provisioning
"""
import os
import json
import logging
import subprocess
import tempfile
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class AnsibleRunner:
    """Manages Ansible playbook execution for demo provisioning"""
    
    def __init__(self, playbook_dir: str):
        """
        Initialize Ansible runner
        
        Args:
            playbook_dir: Directory containing Ansible playbooks
        """
        self.playbook_dir = Path(playbook_dir)
        self.playbook_dir.mkdir(parents=True, exist_ok=True)
    
    def provision_instance(
        self,
        instance_id: str,
        modules: List[str],
        admin_password: str,
        node: Optional[str] = None,
        vmid: Optional[int] = None
    ) -> Dict:
        """
        Provision a new demo instance
        
        Args:
            instance_id: Unique instance identifier
            modules: List of Odoo modules to install
            admin_password: Admin password for Odoo
            node: Proxmox node (auto-selected if None)
            vmid: Container VMID (auto-assigned if None)
        
        Returns:
            Dict with provision results
        """
        try:
            # Create extra vars
            extra_vars = {
                'instance_id': instance_id,
                'odoo_modules': modules,
                'odoo_admin_password': admin_password,
                'proxmox_node': node or 'auto',
                'container_vmid': vmid or 'auto'
            }
            
            # Write extra vars to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                json.dump(extra_vars, f)
                vars_file = f.name
            
            # Run playbook
            playbook_path = self.playbook_dir / 'provision_demo.yml'
            
            cmd = [
                'ansible-playbook',
                str(playbook_path),
                '-e', f'@{vars_file}',
                '--extra-vars', f'ansible_python_interpreter={self._get_python_interpreter()}'
            ]
            
            logger.info(f"Running Ansible playbook for instance {instance_id}")
            logger.debug(f"Command: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout
            )
            
            # Clean up temp file
            os.unlink(vars_file)
            
            if result.returncode == 0:
                logger.info(f"Successfully provisioned instance {instance_id}")
                # Parse output to extract container details
                return self._parse_playbook_output(result.stdout, instance_id)
            else:
                logger.error(f"Ansible playbook failed: {result.stderr}")
                return {
                    'success': False,
                    'error': result.stderr
                }
                
        except subprocess.TimeoutExpired:
            logger.error(f"Ansible playbook timed out for instance {instance_id}")
            return {
                'success': False,
                'error': 'Provisioning timed out'
            }
        except Exception as e:
            logger.error(f"Error running Ansible playbook: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _parse_playbook_output(self, output: str, instance_id: str) -> Dict:
        """Parse Ansible playbook output to extract container details"""
        # Look for JSON output in playbook results
        try:
            # This assumes the playbook outputs a JSON fact with container details
            lines = output.split('\n')
            for line in lines:
                if '"container_info"' in line or '"demo_container"' in line:
                    # Extract JSON from line
                    start = line.find('{')
                    if start >= 0:
                        json_str = line[start:]
                        data = json.loads(json_str)
                        return {
                            'success': True,
                            'instance_id': instance_id,
                            'container_id': data.get('vmid'),
                            'node': data.get('node'),
                            'ip_address': data.get('ip_address')
                        }
        except Exception as e:
            logger.warning(f"Could not parse playbook output: {str(e)}")
        
        # Default response if parsing fails
        return {
            'success': True,
            'instance_id': instance_id,
            'container_id': None,
            'node': None,
            'ip_address': None
        }
    
    def deprovision_instance(self, instance_id: str, node: str, vmid: int) -> Dict:
        """
        Deprovision a demo instance
        
        Args:
            instance_id: Instance identifier
            node: Proxmox node
            vmid: Container VMID
        
        Returns:
            Dict with deprovision results
        """
        try:
            extra_vars = {
                'instance_id': instance_id,
                'proxmox_node': node,
                'container_vmid': vmid
            }
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                json.dump(extra_vars, f)
                vars_file = f.name
            
            playbook_path = self.playbook_dir / 'deprovision_demo.yml'
            
            cmd = [
                'ansible-playbook',
                str(playbook_path),
                '-e', f'@{vars_file}'
            ]
            
            logger.info(f"Deprovisioning instance {instance_id}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            os.unlink(vars_file)
            
            if result.returncode == 0:
                logger.info(f"Successfully deprovisioned instance {instance_id}")
                return {'success': True}
            else:
                logger.error(f"Deprovision failed: {result.stderr}")
                return {'success': False, 'error': result.stderr}
                
        except Exception as e:
            logger.error(f"Error deprovisioning instance: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _get_python_interpreter(self) -> str:
        """Get Python interpreter path"""
        import sys
        return sys.executable
    
    def run_playbook(
        self,
        playbook: str,
        extra_vars: Optional[Dict] = None,
        inventory: Optional[str] = None,
        limit: Optional[str] = None
    ) -> Dict:
        """
        Run an arbitrary Ansible playbook
        
        Args:
            playbook: Playbook name or path
            extra_vars: Extra variables dict
            inventory: Inventory file path
            limit: Limit to specific hosts
        
        Returns:
            Dict with execution results
        """
        try:
            cmd = ['ansible-playbook']
            
            # Playbook path
            if not playbook.endswith('.yml'):
                playbook = f"{playbook}.yml"
            playbook_path = self.playbook_dir / playbook
            cmd.append(str(playbook_path))
            
            # Inventory
            if inventory:
                cmd.extend(['-i', inventory])
            
            # Extra vars
            if extra_vars:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                    json.dump(extra_vars, f)
                    vars_file = f.name
                cmd.extend(['-e', f'@{vars_file}'])
            else:
                vars_file = None
            
            # Limit
            if limit:
                cmd.extend(['--limit', limit])
            
            logger.info(f"Running playbook: {playbook}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600
            )
            
            if vars_file:
                os.unlink(vars_file)
            
            return {
                'success': result.returncode == 0,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode
            }
            
        except Exception as e:
            logger.error(f"Error running playbook: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
