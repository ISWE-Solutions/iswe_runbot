# Nginx Configuration for Separate Web Server

This document provides instructions for configuring Nginx on a separate server to proxy requests to the ISWE Meal Demo Platform.

## Architecture

```
Internet → Nginx Server (Port 80/443) → Application Server (Port 5000)
```

## Prerequisites

- Nginx server with public IP or domain
- Application server running ISWE Meal Demo Platform on port 5000
- Network connectivity between Nginx server and application server

## Step-by-Step Configuration

### 1. On Application Server

#### Configure Firewall

Allow connections from your Nginx server:

```bash
# Allow from specific Nginx server IP
sudo ufw allow from NGINX_SERVER_IP to any port 5000

# Or allow from a network range
sudo ufw allow from 192.168.1.0/24 to any port 5000

# Enable firewall
sudo ufw enable
sudo ufw status
```

#### Verify Application is Running

```bash
# Check service status
sudo systemctl status iswe_meal_demo_platform

# Test local access
curl http://localhost:5000
```

### 2. On Nginx Server

#### Install Nginx

```bash
sudo apt update
sudo apt install nginx
```

#### Create Configuration File

```bash
sudo nano /etc/nginx/sites-available/iswe_meal_demo
```

#### Basic Configuration (HTTP Only)

```nginx
server {
    listen 80;
    server_name demo.example.com;  # Replace with your domain

    # Logging
    access_log /var/log/nginx/iswe_meal_demo_access.log;
    error_log /var/log/nginx/iswe_meal_demo_error.log;

    # Proxy all requests to application server
    location / {
        proxy_pass http://APP_SERVER_IP:5000;  # Replace APP_SERVER_IP
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Timeouts for long-running operations
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
        send_timeout 600;
    }

    # WebSocket support (if needed in future)
    location /websocket {
        proxy_pass http://APP_SERVER_IP:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Increase max upload size
    client_max_body_size 100M;
}
```

#### Enable Site

```bash
# Create symbolic link
sudo ln -s /etc/nginx/sites-available/iswe_meal_demo /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Reload Nginx
sudo systemctl reload nginx
```

### 3. Configure SSL/HTTPS (Recommended)

#### Option A: Let's Encrypt (Free)

```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Obtain certificate and auto-configure Nginx
sudo certbot --nginx -d demo.example.com

# Certificate auto-renewal is configured automatically
# Verify timer is active
sudo systemctl status certbot.timer
```

#### Option B: Manual SSL Configuration

If you have your own SSL certificates:

```nginx
server {
    listen 443 ssl http2;
    server_name demo.example.com;

    # SSL certificates
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;
    
    # Modern SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    
    # HSTS (optional but recommended)
    add_header Strict-Transport-Security "max-age=63072000" always;

    # Rest of proxy configuration...
    location / {
        proxy_pass http://APP_SERVER_IP:5000;
        # ... same as above
    }
}

# HTTP to HTTPS redirect
server {
    listen 80;
    server_name demo.example.com;
    return 301 https://$server_name$request_uri;
}
```

### 4. Advanced Configuration Options

#### Rate Limiting

Protect against abuse:

```nginx
# In http block (/etc/nginx/nginx.conf)
http {
    limit_req_zone $binary_remote_addr zone=demo_limit:10m rate=10r/s;
    
    # ... rest of config
}

# In server block
server {
    location / {
        limit_req zone=demo_limit burst=20 nodelay;
        proxy_pass http://APP_SERVER_IP:5000;
        # ... rest of config
    }
}
```

#### Caching Static Assets

```nginx
location /static/ {
    proxy_pass http://APP_SERVER_IP:5000/static/;
    proxy_cache_valid 200 1d;
    expires 1d;
    add_header Cache-Control "public, immutable";
}
```

#### IP Whitelisting (Optional)

Restrict access to specific IPs:

```nginx
location / {
    allow 192.168.1.0/24;
    allow 10.0.0.0/8;
    deny all;
    
    proxy_pass http://APP_SERVER_IP:5000;
    # ... rest of config
}
```

### 5. DNS Configuration

Point your domain to the Nginx server:

```
Type: A
Name: demo (or @)
Value: NGINX_SERVER_IP
TTL: 3600
```

### 6. Testing

```bash
# Test from internet
curl http://demo.example.com
curl https://demo.example.com

# Check SSL
curl -vI https://demo.example.com

# Monitor logs
sudo tail -f /var/log/nginx/iswe_meal_demo_access.log
sudo tail -f /var/log/nginx/iswe_meal_demo_error.log
```

## Troubleshooting

### Connection Refused

1. Verify application is running on port 5000
2. Check firewall rules on application server
3. Verify network connectivity: `telnet APP_SERVER_IP 5000`

### 502 Bad Gateway

1. Check application server logs: `journalctl -u iswe_meal_demo_platform -f`
2. Verify application is running: `systemctl status iswe_meal_demo_platform`
3. Check Nginx error logs: `tail -f /var/log/nginx/iswe_meal_demo_error.log`

### SSL Certificate Issues

```bash
# Test SSL certificate
sudo certbot certificates

# Renew manually
sudo certbot renew --dry-run
```

### Timeout Issues

If provisioning containers causes timeouts, increase timeouts in Nginx:

```nginx
proxy_connect_timeout 900;
proxy_send_timeout 900;
proxy_read_timeout 900;
send_timeout 900;
```

## Security Best Practices

1. **Always use HTTPS in production**
2. **Keep Nginx updated**: `sudo apt update && sudo apt upgrade nginx`
3. **Configure firewall** on both servers
4. **Enable rate limiting** to prevent abuse
5. **Monitor logs** regularly
6. **Use strong SSL configuration**
7. **Consider using a CDN** like Cloudflare for DDoS protection

## Performance Tuning

### Nginx Worker Processes

```nginx
# /etc/nginx/nginx.conf
worker_processes auto;
worker_connections 1024;
```

### Connection Pooling

```nginx
upstream app_backend {
    server APP_SERVER_IP:5000 max_fails=3 fail_timeout=30s;
    keepalive 32;
}

server {
    location / {
        proxy_pass http://app_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        # ... rest of config
    }
}
```

## Monitoring

### Set up monitoring alerts

```bash
# Check if Nginx is running
systemctl status nginx

# Monitor connections
sudo netstat -plant | grep :80
sudo netstat -plant | grep :443

# View real-time access logs
sudo tail -f /var/log/nginx/iswe_meal_demo_access.log | grep -v "GET /static"
```

## Additional Resources

- [Nginx Documentation](https://nginx.org/en/docs/)
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/)
- [Mozilla SSL Configuration Generator](https://ssl-config.mozilla.org/)

---

**Note**: Replace all placeholders (APP_SERVER_IP, demo.example.com, NGINX_SERVER_IP) with your actual values.
