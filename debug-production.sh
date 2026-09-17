#!/bin/bash

echo "=== Production Backend Debugging Script ==="
echo "Date: $(date)"
echo ""

echo "1. Checking Docker containers status:"
docker ps -a | grep -E "(backend|customer)" || echo "No backend containers found"
echo ""

echo "2. Checking if backend port 8001 is listening:"
netstat -tlnp | grep :8001 || echo "Port 8001 not listening"
echo ""

echo "3. Testing internal backend health check:"
if curl -sf http://localhost:8001/health > /dev/null 2>&1; then
    echo "✅ Internal health check PASSED"
    curl -s http://localhost:8001/health | jq . || curl -s http://localhost:8001/health
else
    echo "❌ Internal health check FAILED"
fi
echo ""

echo "4. Testing backend social-login endpoint internally:"
curl -X POST http://localhost:8001/api/v1/auth/social-login \
  -H "Content-Type: application/json" \
  -d '{"provider": "test", "token": "test"}' \
  -w "\nHTTP Status: %{http_code}\n" \
  --max-time 10 || echo "❌ Internal social-login endpoint failed"
echo ""

echo "5. Testing external backend health check:"
if curl -sf https://autoflowapi.tabloide.pro/health > /dev/null 2>&1; then
    echo "✅ External health check PASSED"
    curl -s https://autoflowapi.tabloide.pro/health | jq . || curl -s https://autoflowapi.tabloide.pro/health
else
    echo "❌ External health check FAILED"
fi
echo ""

echo "6. Testing external social-login endpoint:"
curl -X POST https://autoflowapi.tabloide.pro/api/v1/auth/social-login \
  -H "Content-Type: application/json" \
  -d '{"provider": "test", "token": "test"}' \
  -w "\nHTTP Status: %{http_code}\n" \
  --max-time 10 || echo "❌ External social-login endpoint failed"
echo ""

echo "7. Checking nginx configuration for backend proxy:"
nginx -T 2>/dev/null | grep -A5 -B5 "autoflowapi" || echo "No nginx configuration found for autoflowapi"
echo ""

echo "8. Checking recent backend container logs:"
docker logs --tail=50 $(docker ps -q --filter name=backend) 2>/dev/null || echo "No backend container logs found"
echo ""

echo "9. DNS resolution test:"
nslookup autoflowapi.tabloide.pro || echo "DNS resolution failed"
echo ""

echo "=== Debugging Complete ==="