"""
ISWE Meal Demo Platform
Flask-based web portal for on-demand demo instances
"""
import os
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import secrets
from proxmox_manager import ProxmoxManager
from ansible_runner import AnsibleRunner

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///demo_platform.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Initialize managers
proxmox = ProxmoxManager(
    host=os.environ.get('PROXMOX_HOST'),
    user=os.environ.get('PROXMOX_USER'),
    password=os.environ.get('PROXMOX_PASSWORD'),
    verify_ssl=os.environ.get('PROXMOX_VERIFY_SSL', 'false').lower() == 'true'
)
ansible = AnsibleRunner(playbook_dir='./ansible')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Database Models
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    organization = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    instances = db.relationship('DemoInstance', backref='user', lazy=True)


class DemoInstance(db.Model):
    __tablename__ = 'demo_instances'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    instance_id = db.Column(db.String(50), unique=True, nullable=False)
    container_id = db.Column(db.Integer)
    node = db.Column(db.String(50))
    ip_address = db.Column(db.String(50))
    url = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')  # pending, provisioning, running, stopped, error
    modules = db.Column(db.Text)  # JSON string of selected modules
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)
    admin_password = db.Column(db.String(100))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Routes
@app.route('/')
def index():
    """Landing page"""
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login/registration"""
    if request.method == 'POST':
        data = request.json
        email = data.get('email')
        name = data.get('name')
        organization = data.get('organization')
        
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(email=email, name=name, organization=organization)
            db.session.add(user)
            db.session.commit()
        
        login_user(user)
        return jsonify({'success': True, 'redirect': url_for('dashboard')})
    
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    """User logout"""
    logout_user()
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    """User dashboard showing active instances"""
    instances = DemoInstance.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', instances=instances)


@app.route('/api/create_instance', methods=['POST'])
@login_required
def create_instance():
    """Create a new demo instance"""
    try:
        data = request.json
        modules = data.get('modules', ['meal', 'dhis2_connector'])
        duration_hours = int(data.get('duration', 24))
        
        # Generate unique instance ID
        instance_id = f"meal-demo-{secrets.token_hex(4)}"
        admin_password = secrets.token_urlsafe(12)
        
        # Create database record
        instance = DemoInstance(
            user_id=current_user.id,
            instance_id=instance_id,
            modules=','.join(modules),
            status='provisioning',
            expires_at=datetime.utcnow() + timedelta(hours=duration_hours),
            admin_password=admin_password
        )
        db.session.add(instance)
        db.session.commit()
        
        # Trigger Ansible provisioning in background
        try:
            result = ansible.provision_instance(
                instance_id=instance_id,
                modules=modules,
                admin_password=admin_password
            )
            
            if result['success']:
                instance.container_id = result['container_id']
                instance.node = result['node']
                instance.ip_address = result['ip_address']
                instance.url = f"http://{result['ip_address']}:8069"
                instance.status = 'running'
            else:
                instance.status = 'error'
            
            db.session.commit()
            
        except Exception as e:
            logger.error(f"Provisioning error: {str(e)}")
            instance.status = 'error'
            db.session.commit()
        
        return jsonify({
            'success': True,
            'instance_id': instance_id,
            'status': instance.status,
            'url': instance.url,
            'admin_password': admin_password
        })
        
    except Exception as e:
        logger.error(f"Instance creation error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/instance/<instance_id>/status')
@login_required
def instance_status(instance_id):
    """Get instance status"""
    instance = DemoInstance.query.filter_by(
        instance_id=instance_id,
        user_id=current_user.id
    ).first_or_404()
    
    # Check container status from Proxmox
    if instance.container_id and instance.node:
        try:
            container_status = proxmox.get_container_status(
                node=instance.node,
                vmid=instance.container_id
            )
            instance.status = container_status
            db.session.commit()
        except Exception as e:
            logger.error(f"Error checking container status: {str(e)}")
    
    return jsonify({
        'instance_id': instance.instance_id,
        'status': instance.status,
        'url': instance.url,
        'ip_address': instance.ip_address,
        'created_at': instance.created_at.isoformat(),
        'expires_at': instance.expires_at.isoformat(),
        'modules': instance.modules.split(',') if instance.modules else []
    })


@app.route('/api/instance/<instance_id>/stop', methods=['POST'])
@login_required
def stop_instance(instance_id):
    """Stop a running instance"""
    instance = DemoInstance.query.filter_by(
        instance_id=instance_id,
        user_id=current_user.id
    ).first_or_404()
    
    try:
        proxmox.stop_container(node=instance.node, vmid=instance.container_id)
        instance.status = 'stopped'
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error stopping instance: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/instance/<instance_id>/start', methods=['POST'])
@login_required
def start_instance(instance_id):
    """Start a stopped instance"""
    instance = DemoInstance.query.filter_by(
        instance_id=instance_id,
        user_id=current_user.id
    ).first_or_404()
    
    try:
        proxmox.start_container(node=instance.node, vmid=instance.container_id)
        instance.status = 'running'
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error starting instance: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/instance/<instance_id>/delete', methods=['DELETE'])
@login_required
def delete_instance(instance_id):
    """Delete an instance"""
    instance = DemoInstance.query.filter_by(
        instance_id=instance_id,
        user_id=current_user.id
    ).first_or_404()
    
    try:
        # Delete container from Proxmox
        if instance.container_id and instance.node:
            proxmox.delete_container(node=instance.node, vmid=instance.container_id)
        
        # Delete from database
        db.session.delete(instance)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error deleting instance: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stats')
def stats():
    """Platform statistics"""
    total_instances = DemoInstance.query.count()
    active_instances = DemoInstance.query.filter_by(status='running').count()
    total_users = User.query.count()
    
    return jsonify({
        'total_instances': total_instances,
        'active_instances': active_instances,
        'total_users': total_users
    })


# CLI Commands
@app.cli.command()
def init_db():
    """Initialize the database"""
    db.create_all()
    print("Database initialized!")


@app.cli.command()
def cleanup_expired():
    """Cleanup expired instances"""
    expired = DemoInstance.query.filter(
        DemoInstance.expires_at < datetime.utcnow(),
        DemoInstance.status.in_(['running', 'stopped'])
    ).all()
    
    for instance in expired:
        try:
            if instance.container_id and instance.node:
                proxmox.delete_container(node=instance.node, vmid=instance.container_id)
            db.session.delete(instance)
            logger.info(f"Cleaned up expired instance: {instance.instance_id}")
        except Exception as e:
            logger.error(f"Error cleaning up instance {instance.instance_id}: {str(e)}")
    
    db.session.commit()
    print(f"Cleaned up {len(expired)} expired instances")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
