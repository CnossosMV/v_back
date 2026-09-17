#!/bin/bash

# Backend Full Stack Startup Script
echo "🚀 Starting Backend Full Stack (API + Database + n8n + Evolution API + Vault + Keycloak + Adminer + Redis)..."
echo ""

# Change to script directory
cd "$(dirname "$0")"

# Stop any existing containers
echo "🛑 Stopping existing containers..."
docker compose -p customer down

# Remove any orphaned containers
echo "🧹 Cleaning up orphaned containers..."
docker compose -p customer down --remove-orphans

# Create shared network for all backend services
echo "🌐 Creating shared network..."
docker network create customer_stack_network 2>/dev/null || echo "Network already exists"

# Build and start all services
echo "🏗️  Building and starting all backend services..."
docker compose -p customer up --build -d

# Wait for services to be ready
echo "⏳ Waiting for all services to start (30 seconds)..."
sleep 30

# Check service status
echo "📊 Checking service status..."
docker compose -p customer ps

echo ""
echo "✅ Backend full stack is running!"
echo ""
echo "🌐 Access URLs:"
echo "  - Backend API: http://localhost:8001"
echo "  - API Documentation: http://localhost:8001/docs"
echo "  - Vault (Secret Management): http://localhost:8005"
echo "    Root Token: customer-dev-token-123"
echo "  - Keycloak (Auth): http://localhost:8006"
echo "    Username: admin, Password: admin123"
echo "  - Adminer (Database UI): http://localhost:8290"
echo "    Server: db, Username: postgres, Password: postgres, Database: customer_db"
echo "  - n8n (Automation): http://localhost:5679"
echo "    Username: admin, Password: admin123"
echo "  - Evolution API (WhatsApp): http://localhost:8003"
echo "Sensitive credentials are loaded from environment variables."
echo "  - Evolution Manager: http://localhost:8003/manager"
echo "  - Database: localhost:5433"
echo "  - Redis: localhost:6380"
echo ""
echo "📚 API Documentation:"
echo "  - Customer Management API: http://localhost:8001/docs"
echo "  - Evolution API: http://localhost:8003/docs"
echo ""
echo "💡 Frontend: Run 'npm run dev' in the ../frontend folder"
echo ""
echo "🛠️  Service management:"
echo "  - Stop all services: docker compose -p customer down"
echo "  - Restart all services: docker compose -p customer restart"
echo "  - View logs: docker compose -p customer logs -f [service_name]"
echo "  - Rebuild and restart: docker compose -p customer up --build -d"
echo ""
echo "🔍 Service logs:"
echo "  - Backend API: docker compose -p customer logs -f backend"
echo "  - Database: docker compose -p customer logs -f db"
echo "  - n8n: docker compose -p customer logs -f n8n"
echo "  - Evolution API: docker compose -p customer logs -f evolution-api"
echo ""
