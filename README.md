# ISWE Runbot

A comprehensive web-based platform for provisioning on-demand demo instances of odoo on a Proxmox cluster. Built with Flask, Ansible, and Proxmox API.

> **Note**: This platform is designed to run on a dedicated application server. Nginx should be configured on a **separate web server** for production deployments. See [NGINX_SETUP.md](NGINX_SETUP.md) for detailed Nginx configuration instructions.

## 🌟 Features

- **Instant Provisioning**: Create fully configured Odoo demo environments in minutes
- **LXC Containers**: Lightweight, isolated demo instances using Proxmox LXC technology
- **Automated Deployment**: Ansible playbooks handle complete setup from container creation to Odoo installation
- **Web Portal**: User-friendly Flask interface for managing demo instances
- **Multi-Module Support**: Select and deploy specific ISWE Meal Addons modules
- **Lifecycle Management**: Start, stop, and delete instances on demand
- **Auto-Cleanup**: Scheduled tasks automatically remove expired instances
- **Proxmox Integration**: Direct integration with Proxmox API for cluster management

## 📋 Architecture

```
┌─────────────────┐
│  Flask Portal   │  ← User Interface
│  (Web UI)       │
└────────┬────────┘
         │
         ├──────────────────┬──────────────────┐
         │                  │                  │
┌────────▼────────┐ ┌──────▼──────┐  ┌────────▼────────┐
│ Proxmox Manager │ │Ansible Runner│  │  PostgreSQL DB  │
│  (API Client)   │ │  (Playbooks) │  │   (Instances)   │
└────────┬────────┘ └──────┬──────┘  └─────────────────┘
         │                  │
         └──────────┬───────┘
                    │
         ┌──────────▼──────────┐
         │   Proxmox Cluster   │
         │  (LXC Containers)   │
         └─────────────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Proxmox VE 7.0+ cluster
- Python 3.10+
- Redis (for background tasks)
- PostgreSQL (recommended for production)
- Ansible 2.9+
- Ubuntu 22.04 LXC template on Proxmox

### Installation

1. **Clone the repository**

```bash
cd iswe_runbot
```

2. **Create virtual environment**

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**

```bash
pip install -r requirements.txt
```

4. **Install Ansible collections**

```bash
ansible-galaxy collection install community.general
ansible-galaxy collection install ansible.posix
```

5. **Configure environment**

```bash
cp .env.example .env
nano .env  # Edit with your Proxmox credentials and settings
```

Required environment variables:
```bash
PROXMOX_HOST=https://your-proxmox.example.com:8006
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=your_password
PROXMOX_VERIFY_SSL=false

SECRET_KEY=your_random_secret_key
DATABASE_URL=postgresql://user:pass@localhost/iswe_meal_demo_platform
```

6. **Initialize database**

```bash
flask init-db
```

7. **Start Redis**

```bash
sudo systemctl start redis
```

8. **Run the application**

Development mode:
```bash
python app.py
```

Production mode with Gunicorn:
```bash
gunicorn --workers 4 --bind 0.0.0.0:5000 app:app
```

9. **Start Celery workers** (in separate terminals)

```bash
celery -A tasks worker --loglevel=info
celery -A tasks beat --loglevel=info
```

## 🎯 Usage

### Creating a Demo Instance

1. Navigate to `http://localhost:5000`
2. Click "Get Started" and enter your details
3. On the dashboard, click "New Instance"
4. Select desired modules (Meal Management, DHIS2 Connector)
5. Choose instance duration
6. Click "Create Instance"

The platform will:
- Select an optimal Proxmox node
- Create an LXC container
- Install Ubuntu 22.04
- Set up PostgreSQL
- Clone and install Odoo 19
- Deploy ISWE Meal Addons
- Initialize selected modules
- Provide access credentials

### Managing Instances

- **Start**: Boot up a stopped instance
- **Stop**: Gracefully shutdown a running instance
- **Delete**: Permanently remove an instance
- **Access**: Click "Open Odoo" to access the demo

### API Endpoints

```bash
# Create instance
POST /api/create_instance
{
  "modules": ["meal", "dhis2_connector"],
  "duration": 24
}

# Get instance status
GET /api/instance/<instance_id>/status

# Stop instance
POST /api/instance/<instance_id>/stop

# Start instance
POST /api/instance/<instance_id>/start

# Delete instance
DELETE /api/instance/<instance_id>/delete

# Platform statistics
GET /api/stats
```

## 🔧 Configuration

### Proxmox Setup

1. **Create LXC template**

Download Ubuntu 22.04 template on Proxmox:
```bash
pveam update
pveam download local ubuntu-22.04-standard_22.04-1_amd64.tar.zst
```

2. **Configure network bridge**

Ensure `vmbr0` is configured for container networking.

3. **Set up storage**

Configure `local-lvm` or your preferred storage backend.

### Ansible Customization

Edit [ansible/provision_demo.yml](ansible/provision_demo.yml) to customize:
- Container resources (CPU, RAM, disk)
- Odoo version
- Module installation
- Network configuration

### Module Configuration

Available modules:
- `meal`: Meal Management System
- `dhis2_connector`: DHIS2 Integration

Add more modules by:
1. Updating module list in `app.py`
2. Ensuring modules exist in `iswe_meal_addons/`
3. Adding module to Ansible playbook installation

## 📦 Deployment

### Production Deployment

1. **Install system services**

```bash
sudo cp iswe_meal_demo_platform.service /etc/systemd/system/
sudo cp celery.service /etc/systemd/system/
sudo cp celery-beat.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable iswe_meal_demo_platform celery celery-beat
sudo systemctl start iswe_meal_demo_platform celery celery-beat
```

2. **Configure firewall on application server**

Allow access from your Nginx server:

```bash
# Allow from specific Nginx server
sudo ufw allow from NGINX_SERVER_IP to any port 5000

# Or allow from a network range
sudo ufw allow from 192.168.1.0/24 to any port 5000

sudo ufw enable
```

3. **Configure Nginx on your separate web server**

On your Nginx server, create a configuration file:

```bash
sudo nano /etc/nginx/sites-available/iswe_meal_demo
```

Add the following configuration (replace `YOUR_APP_SERVER_IP` with the actual IP):

```nginx
server {
    listen 80;
    server_name demo.example.com;

    # Logging
    access_log /var/log/nginx/iswe_meal_demo_access.log;
    error_log /var/log/nginx/iswe_meal_demo_error.log;

    # Proxy settings
    location / {
        proxy_pass http://YOUR_APP_SERVER_IP:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Timeouts for long-running operations (container provisioning)
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
        send_timeout 600;
    }

    # WebSocket support (if needed)
    location /websocket {
        proxy_pass http://YOUR_APP_SERVER_IP:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Increase client max body size for file uploads
    client_max_body_size 100M;
}
```

Enable the site and test configuration:

```bash
sudo ln -s /etc/nginx/sites-available/iswe_meal_demo /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

4. **Configure SSL with Let's Encrypt (Recommended)**

On your Nginx server:

```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Obtain and configure SSL certificate
sudo certbot --nginx -d demo.example.com

# Certificates will auto-renew via systemd timer
sudo systemctl status certbot.timer
```

**Alternative: Manual SSL Configuration**

If you already have SSL certificates:

```nginx
server {
    listen 443 ssl http2;
    server_name demo.example.com;

    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;
    
    # Modern SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    
    # Rest of configuration...
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name demo.example.com;
    return 301 https://$server_name$request_uri;
}
```

### Database Migration

For production, use PostgreSQL:

```bash
# Install PostgreSQL
sudo apt install postgresql postgresql-contrib

# Create database
sudo -u postgres createdb iswe_meal_demo_platform
sudo -u postgres createuser demo_user

# Update .env
DATABASE_URL=postgresql://demo_user:password@localhost/iswe_meal_demo_platform
```

## 🛠️ Maintenance

### Cleanup Expired Instances

Manual cleanup:
```bash
flask cleanup-expired
```

Automatic cleanup runs hourly via Celery Beat.

### Monitor Instances

```bash
# Check container status on Proxmox
pct list

# View logs
tail -f /var/log/odoo/odoo.log

# Check Flask logs
journalctl -u iswe_meal_demo_platform -f
```

### Backup

Backup database:
```bash
pg_dump iswe_meal_demo_platform > backup.sql
```

Backup Proxmox containers:
```bash
vzdump <vmid> --storage local
```

## 📚 Project Structure

```
iswe_meal_demo_platform/
├── app.py                    # Flask application
├── proxmox_manager.py        # Proxmox API client
├── ansible_runner.py         # Ansible execution wrapper
├── tasks.py                  # Celery background tasks
├── requirements.txt          # Python dependencies
├── .env.example              # Environment template
├── ansible/
│   ├── provision_demo.yml    # Container provisioning playbook
│   ├── deprovision_demo.yml  # Container cleanup playbook
│   ├── ansible.cfg           # Ansible configuration
│   └── templates/
│       ├── odoo.conf.j2      # Odoo configuration template
│       └── odoo.service.j2   # Systemd service template
├── templates/
│   ├── index.html            # Landing page
│   ├── login.html            # Login/registration
│   └── dashboard.html        # User dashboard
├── *.service                 # Systemd service files
└── README.md                 # This file
```

## 🔐 Security Considerations

- Change default `SECRET_KEY` in production
- Use strong Proxmox credentials
- Enable SSL for Proxmox API
- Implement rate limiting for API endpoints
- Set up firewall rules for container network
- Use PostgreSQL instead of SQLite in production
- Enable HTTPS for Flask application
- Implement user authentication/authorization
- Regularly update Odoo and system packages

## 🐛 Troubleshooting

### Container creation fails

Check Proxmox logs:
```bash
journalctl -u pvedaemon -f
```

Verify template exists:
```bash
pveam list local
```

### Ansible playbook timeout

Increase timeout in `ansible_runner.py`:
```python
timeout=1200  # 20 minutes
```

### Database connection errors

Check PostgreSQL status:
```bash
sudo systemctl status postgresql
```

Verify DATABASE_URL in `.env`

### Container can't access internet

Check Proxmox network configuration:
```bash
cat /etc/network/interfaces
```

Verify NAT/routing is configured correctly.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📄 License

This project is licensed under the same license as the ISWE Meal Addons modules.

## 👥 Support

For issues and questions:
- GitHub Issues: https://github.com/ISWE-Solutions/iswe_meal_addons/issues
- Documentation: See `doc/` directory

## 🙏 Acknowledgments

- Odoo Community
- Proxmox VE Team
- Ansible Community
- ISWE Solutions Team

## 📊 System Requirements

### Server Requirements

**Minimum:**
- CPU: 4 cores
- RAM: 8 GB
- Storage: 100 GB SSD
- Network: 100 Mbps

**Recommended:**
- CPU: 8+ cores
- RAM: 16+ GB
- Storage: 500 GB SSD
- Network: 1 Gbps

### Per Container Requirements

- CPU: 2 cores
- RAM: 4 GB
- Disk: 20 GB
- Network: Bridged to vmbr0

## 🔄 Updates

To update the platform:

```bash
cd iswe_meal_demo_platform
git pull
source venv/bin/activate
pip install -r requirements.txt --upgrade
flask db upgrade  # If using migrations
sudo systemctl restart iswe_meal_demo_platform celery celery-beat
```

---

**Built with ❤️ for the ISWE Meal Addons Community**
