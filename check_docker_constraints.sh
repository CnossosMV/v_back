#!/bin/bash
# Docker database constraint verification script
# Ensures alembic_version table has proper varchar(32) constraint

echo "🐳 Checking Docker database constraints..."

# Check if database container is running
if ! docker ps | grep -qE "customer-db-1|backend-db-1|versya-db-1|customer-backend_db_1"; then
    echo "❌ Database container is not running"
    echo "Please start your Docker containers first"
    exit 1
fi

# Try different container names (in order of preference)
DB_CONTAINER=""
if docker ps | grep -q "customer-db-1"; then
    DB_CONTAINER="customer-db-1"
elif docker ps | grep -q "backend-db-1"; then
    DB_CONTAINER="backend-db-1"
elif docker ps | grep -q "versya-db-1"; then
    DB_CONTAINER="versya-db-1"
elif docker ps | grep -q "customer-backend_db_1"; then
    DB_CONTAINER="customer-backend_db_1"
else
    echo "❌ Cannot find database container"
    exit 1
fi

echo "📊 Using database container: $DB_CONTAINER"

# Check alembic_version table structure
echo "🔍 Checking alembic_version table structure..."
docker exec $DB_CONTAINER psql -U postgres -d versya -c "\d alembic_version"

if [ $? -eq 0 ]; then
    echo "✅ Database connection successful"

    # Verify varchar(32) constraint
    CONSTRAINT_CHECK=$(docker exec $DB_CONTAINER psql -U postgres -d versya -t -c "SELECT character_maximum_length FROM information_schema.columns WHERE table_name='alembic_version' AND column_name='version_num';")

    if [ "$CONSTRAINT_CHECK" == "32" ]; then
        echo "✅ alembic_version.version_num has correct varchar(32) constraint"
    else
        echo "❌ alembic_version.version_num constraint is not varchar(32)"
        echo "Current constraint: $CONSTRAINT_CHECK"
        echo "This may cause issues with long revision IDs in production"
    fi
else
    echo "❌ Cannot connect to database"
    exit 1
fi
