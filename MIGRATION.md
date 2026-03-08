# Project Rename Summary

The project has been renamed from `demo_platform` to `iswe_meal_demo_platform`.

## What Changed

### Directory Structure
- Old: `/opt/demo_platform`
- New: `/opt/iswe_meal_demo_platform`

### Service Files
- Old: `demo_platform.service`
- New: `iswe_meal_demo_platform.service`

### Database Name
- Old: `demo_platform`
- New: `iswe_meal_demo_platform`

### URLs and References
All documentation and configuration files have been updated to reflect the new name.

## Nginx Configuration

**Important**: Nginx is now configured on a **separate server**, not on the application server.

### Setup Process

1. **Application Server** (runs the Flask app)
   - Runs on port 5000
   - No Nginx installed
   - Firewall configured to allow connections from Nginx server

2. **Nginx Server** (separate server)
   - Proxies requests to application server
   - Handles SSL/TLS
   - Public-facing server

See [NGINX_SETUP.md](NGINX_SETUP.md) for detailed configuration instructions.

## Migration from Old Setup

If you have an existing installation:

```bash
# Stop services
sudo systemctl stop demo_platform celery celery-beat

# Rename directory
sudo mv /opt/demo_platform /opt/iswe_meal_demo_platform

# Update database
sudo -u postgres psql
ALTER DATABASE demo_platform RENAME TO iswe_meal_demo_platform;
\q

# Update service files
sudo mv /etc/systemd/system/demo_platform.service /etc/systemd/system/iswe_meal_demo_platform.service
sudo nano /etc/systemd/system/iswe_meal_demo_platform.service
# Update paths: /opt/demo_platform → /opt/iswe_meal_demo_platform

# Update celery service
sudo nano /etc/systemd/system/celery.service
# Update WorkingDirectory and Environment paths

# Update celery-beat service
sudo nano /etc/systemd/system/celery-beat.service
# Update WorkingDirectory and Environment paths

# Update .env file
cd /opt/iswe_meal_demo_platform
nano .env
# Update DATABASE_URL to use iswe_meal_demo_platform

# Reload systemd
sudo systemctl daemon-reload

# Start services
sudo systemctl start iswe_meal_demo_platform celery celery-beat

# Verify
sudo systemctl status iswe_meal_demo_platform
```

## Quick Start for New Installation

```bash
# Clone and navigate
cd odoo-19.0.post20260108/iswe_meal_demo_platform

# Run setup script
sudo bash setup.sh

# Configure Proxmox credentials
sudo nano /opt/iswe_meal_demo_platform/.env

# Restart services
sudo systemctl restart iswe_meal_demo_platform celery celery-beat

# Configure Nginx on separate server (see NGINX_SETUP.md)
```

## Service Management

```bash
# Check status
sudo systemctl status iswe_meal_demo_platform
sudo systemctl status celery
sudo systemctl status celery-beat

# View logs
sudo journalctl -u iswe_meal_demo_platform -f
sudo journalctl -u celery -f

# Restart
sudo systemctl restart iswe_meal_demo_platform celery celery-beat
```

## Configuration Files

All service files and configurations use the new project name:
- `iswe_meal_demo_platform.service`
- `celery.service`
- `celery-beat.service`

No local Nginx configuration - see [NGINX_SETUP.md](NGINX_SETUP.md) for external server setup.
