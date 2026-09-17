"""
Database models for Smart Analytics system
"""

from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

# Use shared Base from app.database to ensure all models are in the same metadata
from app.database import Base

class TenantSchema(Base):
    """
    Stores the analytics schema configured by each user/tenant
    """
    __tablename__ = "tenant_schemas"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    schema_name = Column(String(255), nullable=False)
    description = Column(Text)
    fields = Column(JSON, nullable=False)  # JSONB in PostgreSQL
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    __table_args__ = (
        Index('idx_tenant_schemas_tenant_id', 'tenant_id'),
    )

class TenantEvent(Base):
    """
    Stores all tracking events from user websites
    Optimized for high-frequency inserts and fast queries
    """
    __tablename__ = "tenant_events"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    user_id = Column(String(255), index=True)  # Anonymous or identified user
    event_data = Column(JSONB, nullable=False)  # JSONB for GIN index support
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    __table_args__ = (
        # Composite indexes for common queries
        Index('idx_tenant_events_tenant_date', 'tenant_id', 'created_at'),
        Index('idx_tenant_events_user_date', 'tenant_id', 'user_id', 'created_at'),
        
        # GIN index on JSONB event_data for fast queries
        Index('idx_tenant_events_data_gin', 'event_data', postgresql_using='gin'),
        
        # Partial index for specific event types (common queries)
        Index('idx_tenant_events_button_clicks', 
              'tenant_id', 'created_at',
              postgresql_where="event_data->>'event' = 'button_click'"),
    )

class TenantAutomation(Base):
    """
    Stores automation rules created by users
    IF-THEN rules for email notifications, webhooks, etc.
    """
    __tablename__ = "tenant_automations"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    rule_name = Column(String(255), nullable=False)
    trigger = Column(JSON, nullable=False)  # Trigger conditions
    actions = Column(JSON, nullable=False)  # Actions to take
    active = Column(Boolean, default=True, index=True)
    last_triggered = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    __table_args__ = (
        Index('idx_tenant_automations_active', 'tenant_id', 'active'),
    )

class TenantAPIKey(Base):
    """
    API keys for each tenant's tracking
    """
    __tablename__ = "tenant_api_keys"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    api_key = Column(String(255), nullable=False, unique=True, index=True)
    key_name = Column(String(255))  # Human-readable name
    is_active = Column(Boolean, default=True, index=True)
    last_used = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))  # Optional expiration

class TenantSettings(Base):
    """
    Stores configuration settings for each tenant's universal SDK
    """
    __tablename__ = "tenant_settings"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, unique=True, index=True)
    settings = Column(JSON, nullable=False)  # SDK configuration settings
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class TenantMarkedElement(Base):
    """
    Stores elements marked via Chrome extension for tracking
    """
    __tablename__ = "tenant_marked_elements"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    selector = Column(String(500), nullable=False)  # CSS selector for the element
    tracking_type = Column(String(100), nullable=False)  # Type of tracking (button_click, form_submit, etc.)
    friendly_name = Column(String(255), nullable=False)  # Human-readable name
    element_data = Column(JSON, nullable=False)  # Additional element metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    __table_args__ = (
        # Unique constraint: one selector per tenant
        Index('idx_tenant_marked_elements_unique', 'tenant_id', 'selector', unique=True),
        Index('idx_tenant_marked_elements_tenant_type', 'tenant_id', 'tracking_type'),
    )

class TenantAchievement(Base):
    """
    Track achievements and milestones for gamification
    """
    __tablename__ = "tenant_achievements"
    
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    user_id = Column(String(255), index=True)  # Optional: per-user achievements
    achievement_type = Column(String(100), nullable=False)  # 'first_100_clicks', 'week_streak', etc.
    achievement_data = Column(JSON)  # Additional data about the achievement
    unlocked_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('idx_tenant_achievements_tenant_type', 'tenant_id', 'achievement_type'),
    )

# Database creation script for reference
CREATE_TABLES_SQL = """
-- Enable JSONB extension for PostgreSQL
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Tenant Schemas
CREATE TABLE IF NOT EXISTS tenant_schemas (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    schema_name VARCHAR(255) NOT NULL,
    description TEXT,
    fields JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_schemas_tenant_id ON tenant_schemas(tenant_id);

-- Tenant Events (optimized for high-frequency inserts)
CREATE TABLE IF NOT EXISTS tenant_events (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    user_id VARCHAR(255),
    event_data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_events_tenant_date ON tenant_events(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tenant_events_user_date ON tenant_events(tenant_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tenant_events_data_gin ON tenant_events USING GIN (event_data);

-- Partial index for button clicks (common query)
CREATE INDEX IF NOT EXISTS idx_tenant_events_button_clicks 
ON tenant_events(tenant_id, created_at) 
WHERE event_data->>'event' = 'button_click';

-- Tenant Automations
CREATE TABLE IF NOT EXISTS tenant_automations (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    rule_name VARCHAR(255) NOT NULL,
    trigger JSONB NOT NULL,
    actions JSONB NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    last_triggered TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_automations_active ON tenant_automations(tenant_id, active);

-- Tenant API Keys
CREATE TABLE IF NOT EXISTS tenant_api_keys (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    api_key VARCHAR(255) NOT NULL UNIQUE,
    key_name VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    last_used TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_tenant_api_keys_tenant_id ON tenant_api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_api_keys_api_key ON tenant_api_keys(api_key) WHERE is_active = TRUE;

-- Tenant Settings (for universal SDK configuration)
CREATE TABLE IF NOT EXISTS tenant_settings (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL UNIQUE,
    settings JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_settings_tenant_id ON tenant_settings(tenant_id);

-- Tenant Marked Elements (Chrome Extension)
CREATE TABLE IF NOT EXISTS tenant_marked_elements (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    selector VARCHAR(500) NOT NULL,
    tracking_type VARCHAR(100) NOT NULL,
    friendly_name VARCHAR(255) NOT NULL,
    element_data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_marked_elements_unique ON tenant_marked_elements(tenant_id, selector);
CREATE INDEX IF NOT EXISTS idx_tenant_marked_elements_tenant_type ON tenant_marked_elements(tenant_id, tracking_type);

-- Tenant Achievements
CREATE TABLE IF NOT EXISTS tenant_achievements (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    user_id VARCHAR(255),
    achievement_type VARCHAR(100) NOT NULL,
    achievement_data JSONB,
    unlocked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_achievements_tenant_type ON tenant_achievements(tenant_id, achievement_type);

-- Sample materialized view for dashboard performance
CREATE MATERIALIZED VIEW IF NOT EXISTS daily_analytics_summary AS
SELECT 
    tenant_id,
    DATE(created_at) as date,
    event_data->>'event' as event_type,
    COUNT(*) as event_count,
    COUNT(DISTINCT user_id) as unique_users,
    COUNT(DISTINCT event_data->>'session_id') as unique_sessions
FROM tenant_events
WHERE created_at >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY tenant_id, DATE(created_at), event_data->>'event';

CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_analytics_summary_unique 
ON daily_analytics_summary(tenant_id, date, event_type);

-- Refresh materialized view function (call this periodically)
-- REFRESH MATERIALIZED VIEW CONCURRENTLY daily_analytics_summary;
"""