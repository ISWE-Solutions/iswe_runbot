#!/bin/bash
# Quick setup script for ISWE Meal Demo Platform

set -e

echo "==================================="
echo "ISWE Meal Demo Platform Setup"
echo "==================================="
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Install system dependencies
echo "Installing system dependencies..."
apt-get update
apt-get install -y python3 python3-pip python3-venv git postgresql postgresql-contrib redis-server ansible

# Create demo user
echo "Creating demo user..."
if ! id "demo" &>/dev/null; then
    useradd -m -s /bin/bash demo
fi

# Set up application directory
APP_DIR="/opt/iswe_meal_demo_platform"
echo "Setting up application directory at $APP_DIR..."
mkdir -p $APP_DIR
cp -r . $APP_DIR/
chown -R demo:demo $APP_DIR

# Create virtual environment
echo "Creating Python virtual environment..."
su - demo -c "cd $APP_DIR && python3 -m venv venv"

# Install Python dependencies
echo "Installing Python dependencies..."
su - demo -c "cd $APP_DIR && source venv/bin/activate && pip install -r requirements.txt"

# Install Ansible collections
echo "Installing Ansible collections..."
su - demo -c "ansible-galaxy collection install community.general"
su - demo -c "ansible-galaxy collection install ansible.posix"

# Set up PostgreSQL
echo "Setting up PostgreSQL database..."
su - postgres -c "psql -c \"CREATE DATABASE iswe_meal_demo_platform;\" || true"
su - postgres -c "psql -c \"CREATE USER demo_user WITH PASSWORD 'demo_password';\" || true"
su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE iswe_meal_demo_platform TO demo_user;\""

# Create .env file if it doesn't exist
if [ ! -f "$APP_DIR/.env" ]; then
    echo "Creating .env file..."
    cp $APP_DIR/.env.example $APP_DIR/.env
    
    # Generate secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s/your_secret_key_here_change_in_production/$SECRET_KEY/" $APP_DIR/.env
    sed -i "s|sqlite:///demo_platform.db|postgresql://demo_user:demo_password@localhost/iswe_meal_demo_platform|" $APP_DIR/.env
    
    echo "IMPORTANT: Edit $APP_DIR/.env with your Proxmox credentials!"
    chown demo:demo $APP_DIR/.env
fi

# Initialize database
echo "Initializing database..."
cd $APP_DIR
su - demo -c "cd $APP_DIR && source venv/bin/activate && flask init-db"

# Install systemd services
echo "Installing systemd services..."
cp $APP_DIR/iswe_meal_demo_platform.service /etc/systemd/system/
cp $APP_DIR/celery.service /etc/systemd/system/
cp $APP_DIR/celery-beat.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable and start services
echo "Starting services..."
systemctl enable redis-server postgresql
systemctl start redis-server postgresql

systemctl enable iswe_meal_demo_platform celery celery-beat
systemctl start iswe_meal_demo_platform celery celery-beat

echo
echo "==================================="
echo "Setup complete!"
echo "==================================="
echo
echo "Next steps:"
echo "1. Edit /opt/iswe_meal_demo_platform/.env with your Proxmox credentials"
echo "2. Restart services: systemctl restart iswe_meal_demo_platform celery celery-beat"
echo "3. Configure Nginx on your separate web server (see README.md)"
echo "4. The platform runs on port 5000 - ensure firewall allows access from your Nginx server"
echo
echo "Service status:"
systemctl status iswe_meal_demo_platform --no-pager -l
echo
echo "View logs with: journalctl -u iswe_meal_demo_platform -f"
