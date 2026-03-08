"""
Celery tasks for background processing
"""
from celery import Celery
import os
from proxmox_manager import ProxmoxManager
from ansible_runner import AnsibleRunner

# Initialize Celery
celery = Celery(
    'iswe_meal_demo_platform',
    broker=os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
)

celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)


@celery.task(name='tasks.provision_instance')
def provision_instance_task(instance_id, modules, admin_password):
    """Background task to provision a demo instance"""
    ansible = AnsibleRunner(playbook_dir='./ansible')
    
    result = ansible.provision_instance(
        instance_id=instance_id,
        modules=modules,
        admin_password=admin_password
    )
    
    return result


@celery.task(name='tasks.cleanup_expired_instances')
def cleanup_expired_instances():
    """Scheduled task to cleanup expired instances"""
    from app import app, db, DemoInstance
    from datetime import datetime
    
    with app.app_context():
        expired = DemoInstance.query.filter(
            DemoInstance.expires_at < datetime.utcnow(),
            DemoInstance.status.in_(['running', 'stopped'])
        ).all()
        
        proxmox = ProxmoxManager(
            host=os.environ.get('PROXMOX_HOST'),
            user=os.environ.get('PROXMOX_USER'),
            password=os.environ.get('PROXMOX_PASSWORD'),
            verify_ssl=False
        )
        
        cleaned = 0
        for instance in expired:
            try:
                if instance.container_id and instance.node:
                    proxmox.delete_container(
                        node=instance.node,
                        vmid=instance.container_id
                    )
                db.session.delete(instance)
                cleaned += 1
            except Exception as e:
                print(f"Error cleaning up instance {instance.instance_id}: {str(e)}")
        
        db.session.commit()
        return {'cleaned': cleaned}


# Configure periodic tasks
celery.conf.beat_schedule = {
    'cleanup-expired-instances': {
        'task': 'tasks.cleanup_expired_instances',
        'schedule': 3600.0,  # Run every hour
    },
}
