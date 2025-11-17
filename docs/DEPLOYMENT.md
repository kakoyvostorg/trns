# TRNS Deployment Guide

This guide covers deploying TRNS to cloud platforms, with a focus on Yandex Cloud.

## Yandex Cloud Deployment

### Option 1: Serverless Containers (Recommended)

Yandex Serverless Containers is ideal for webhook-based bots as it provides:
- Auto-scaling
- Pay-per-use pricing
- Managed service (no VM management)
- Docker support

#### Prerequisites

1. Yandex Cloud account
2. Yandex Cloud CLI installed: `yc init`
3. Docker installed locally

#### Steps

1. **Build Docker image:**
   ```bash
   docker build -f docker/Dockerfile -t trns:latest .
   ```

2. **Push to Yandex Container Registry:**
   ```bash
   # Create registry (if not exists)
   yc container registry create --name trns-registry
   
   # Get registry ID
   REGISTRY_ID=$(yc container registry get trns-registry --format json | jq -r '.id')
   
   # Configure Docker to use Yandex registry
   yc container registry configure-docker
   
   # Tag and push
   docker tag trns:latest cr.yandex/${REGISTRY_ID}/trns:latest
   docker push cr.yandex/${REGISTRY_ID}/trns:latest
   ```

3. **Create Serverless Container:**
   ```bash
   yc serverless container create --name trns-bot \
     --memory 2GB \
     --cores 2 \
     --execution-timeout 300s \
     --concurrency 10 \
     --service-account-id YOUR_SERVICE_ACCOUNT_ID
   ```

4. **Create Container Revision:**
   ```bash
   yc serverless container revision deploy \
     --container-name trns-bot \
     --image cr.yandex/${REGISTRY_ID}/trns:latest \
     --environment BOT_TOKEN=your_token,AUTH_KEY=your_key,OPENROUTER_API_KEY=your_key,ALLOWED_USER_IDS=123456789 \
     --service-account-id YOUR_SERVICE_ACCOUNT_ID
   ```

5. **Get Container URL:**
   ```bash
   yc serverless container get --name trns-bot --format json | jq -r '.url'
   ```

6. **Set Telegram Webhook:**
   ```bash
   CONTAINER_URL=$(yc serverless container get --name trns-bot --format json | jq -r '.url')
   curl -X POST "${CONTAINER_URL}/set_webhook" \
     -H "Content-Type: application/json" \
     -d "{\"webhook_url\": \"${CONTAINER_URL}/webhook\"}"
   ```

#### Using Yandex Lockbox for Secrets

For production, use Yandex Lockbox to store secrets:

1. **Create secret in Lockbox:**
   ```bash
   yc lockbox secret create --name trns-secrets \
     --payload '[{"key": "BOT_TOKEN", "text_value": "your_token"}, ...]'
   ```

2. **Grant access to service account:**
   ```bash
   yc lockbox secret add-access-binding trns-secrets \
     --role lockbox.payloadViewer \
     --service-account-id YOUR_SERVICE_ACCOUNT_ID
   ```

3. **Update container with secret:**
   ```bash
   yc serverless container revision deploy \
     --container-name trns-bot \
     --image cr.yandex/${REGISTRY_ID}/trns:latest \
     --secret environment-variable,BOT_TOKEN,trns-secrets,BOT_TOKEN \
     --secret environment-variable,OPENROUTER_API_KEY,trns-secrets,OPENROUTER_API_KEY
   ```

### Option 2: Compute Cloud (VM)

For more control, deploy to a VM:

1. **Create VM:**
   ```bash
   yc compute instance create \
     --name trns-vm \
     --zone ru-central1-a \
     --network-interface subnet-name=default,nat-ip-version=ipv4 \
     --create-boot-disk image-folder-id=standard-images,image-family=ubuntu-2204,size=20 \
     --ssh-key ~/.ssh/id_rsa.pub
   ```

2. **SSH into VM:**
   ```bash
   ssh ubuntu@$(yc compute instance get trns-vm --format json | jq -r '.network_interfaces[0].primary_v4_address.one_to_one_nat.address')
   ```

3. **Install dependencies:**
   ```bash
   sudo apt-get update
   sudo apt-get install -y python3-pip ffmpeg git
   ```

4. **Clone and install:**
   ```bash
   git clone https://github.com/yourusername/trns.git
   cd trns
   pip3 install -e .
   ```

5. **Create systemd service:**
   ```bash
   sudo nano /etc/systemd/system/trns-bot.service
   ```
   
   Content:
   ```ini
   [Unit]
   Description=TRNS Telegram Bot
   After=network.target
   
   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/trns
   Environment="BOT_TOKEN=your_token"
   Environment="AUTH_KEY=your_key"
   Environment="OPENROUTER_API_KEY=your_key"
   Environment="ALLOWED_USER_IDS=123456789"
   ExecStart=/usr/bin/python3 -m trns.bot.server
   Restart=always
   
   [Install]
   WantedBy=multi-user.target
   ```

6. **Start service:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable trns-bot
   sudo systemctl start trns-bot
   ```

7. **Configure Nginx (optional):**
   ```bash
   sudo apt-get install -y nginx
   sudo nano /etc/nginx/sites-available/trns
   ```
   
   Content:
   ```nginx
   server {
       listen 80;
       server_name your-domain.com;
       
       location / {
           proxy_pass http://localhost:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```
   
   ```bash
   sudo ln -s /etc/nginx/sites-available/trns /etc/nginx/sites-enabled/
   sudo nginx -t
   sudo systemctl restart nginx
   ```

## Docker Deployment

### Using docker-compose

1. **Create `.env` file:**
   ```bash
   BOT_TOKEN=your_token
   AUTH_KEY=your_key
   OPENROUTER_API_KEY=your_key
   ALLOWED_USER_IDS=123456789
   ```

2. **Run:**
   ```bash
   docker-compose -f docker/docker-compose.yml up -d
   ```

### Manual Docker

```bash
docker run -d \
  --name trns-bot \
  -p 8000:8000 \
  -e BOT_TOKEN=your_token \
  -e AUTH_KEY=your_key \
  -e OPENROUTER_API_KEY=your_key \
  -e ALLOWED_USER_IDS=123456789 \
  -v $(pwd)/config:/app/config \
  trns:latest
```

## Health Checks

The bot provides a health check endpoint:

```bash
curl http://localhost:8000/health
```

Response:
```json
{
  "status": "healthy",
  "bot": "running"
}
```

## Monitoring

### Logs

**Serverless Containers:**
```bash
yc serverless container logs --name trns-bot --tail
```

**VM/systemd:**
```bash
sudo journalctl -u trns-bot -f
```

**Docker:**
```bash
docker logs -f trns-bot
```

### Metrics

Monitor:
- Request count
- Response times
- Error rates
- Memory usage
- CPU usage

Use Yandex Cloud Monitoring or external services like Prometheus.

## Scaling

### Serverless Containers

Auto-scales automatically based on load. Configure:
- `--concurrency`: Max concurrent requests per instance
- `--memory`: Memory per instance
- `--cores`: CPU cores per instance

### VM

Use Yandex Application Load Balancer for multiple VMs or scale manually.

## Security Best Practices

1. **Use environment variables** instead of files for secrets
2. **Use Yandex Lockbox** for production secrets
3. **Enable HTTPS** (use Yandex Application Load Balancer or Cloudflare)
4. **Restrict allowed user IDs** in `ALLOWED_USER_IDS`
5. **Regular updates:** Keep dependencies updated
6. **Monitor logs** for suspicious activity

## Troubleshooting

### Container won't start

- Check logs: `yc serverless container logs --name trns-bot`
- Verify environment variables are set
- Check container resource limits

### Webhook not working

- Verify webhook URL is accessible from internet
- Check Telegram webhook info: `curl http://your-url/webhook_info`
- Ensure HTTPS is used (Telegram requires HTTPS for webhooks)

### High memory usage

- Increase container memory: `--memory 4GB`
- Optimize transcription settings in config
- Consider processing videos in smaller chunks

## Cost Optimization

- Use Serverless Containers for pay-per-use
- Set appropriate concurrency limits
- Use caching for repeated transcriptions
- Monitor and optimize resource usage

