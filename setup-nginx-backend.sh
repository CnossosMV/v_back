#!/bin/bash

echo "=== Setting up Nginx Configuration for Backend API ==="

# Check if nginx is installed
if ! command -v nginx &> /dev/null; then
    echo "❌ Nginx is not installed. Please install nginx first."
    exit 1
fi

# Create nginx configuration for backend API
echo "📝 Creating nginx configuration for autoflowapi.tabloide.pro..."

# Copy the configuration file to nginx sites-available
sudo cp nginx-backend-api.conf /etc/nginx/sites-available/backend-api.conf

# Create symbolic link in sites-enabled
sudo ln -sf /etc/nginx/sites-available/backend-api.conf /etc/nginx/sites-enabled/

# Test nginx configuration
echo "🧪 Testing nginx configuration..."
if sudo nginx -t; then
    echo "✅ Nginx configuration is valid"
    
    # Reload nginx
    echo "🔄 Reloading nginx..."
    sudo systemctl reload nginx
    
    echo "✅ Nginx configuration updated successfully!"
    echo ""
    echo "📋 Next steps:"
    echo "1. Ensure the backend container is running on port 8001"
    echo "2. Test the API: curl https://autoflowapi.tabloide.pro/health"
    echo "3. Check nginx logs: sudo tail -f /var/log/nginx/backend-api.error.log"
    
else
    echo "❌ Nginx configuration test failed"
    echo "Please check the configuration and try again"
    exit 1
fi

echo "=== Setup Complete ==="