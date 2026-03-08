#!/usr/bin/env python3
"""
Test script for Proxmox connection
"""
import os
from dotenv import load_dotenv
from proxmox_manager import ProxmoxManager

load_dotenv()

def test_connection():
    print("Testing Proxmox connection...")
    
    try:
        proxmox = ProxmoxManager(
            host=os.environ.get('PROXMOX_HOST'),
            user=os.environ.get('PROXMOX_USER'),
            password=os.environ.get('PROXMOX_PASSWORD'),
            verify_ssl=False
        )
        
        print("✓ Authentication successful")
        
        # Get nodes
        nodes = proxmox.get_nodes()
        print(f"✓ Found {len(nodes)} nodes:")
        for node in nodes:
            print(f"  - {node['node']} (Status: {node['status']}, CPU: {node.get('cpu', 0)*100:.1f}%)")
        
        # Get next VMID
        next_vmid = proxmox.get_next_vmid()
        print(f"✓ Next available VMID: {next_vmid}")
        
        # List containers on first node
        if nodes:
            first_node = nodes[0]['node']
            containers = proxmox.list_containers(first_node)
            print(f"✓ Found {len(containers)} containers on node '{first_node}'")
            for container in containers[:5]:  # Show first 5
                print(f"  - VMID {container['vmid']}: {container.get('name', 'N/A')} (Status: {container.get('status', 'N/A')})")
        
        print("\n✓ All tests passed!")
        return True
        
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return False

if __name__ == '__main__':
    test_connection()
