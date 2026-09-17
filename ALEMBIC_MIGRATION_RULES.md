# Alembic Migration Rules & Best Practices

**CRITICAL: Always reference this file when creating or modifying alembic migrations!**

## 🚨 Character Limits (Database Constraints)

### Revision ID Length
- **MAXIMUM: 32 characters** (PostgreSQL varchar(32) constraint)
- **Format**: `###_short_descriptive_name`
- **Test**: `len("your_revision_id") <= 32`

### Filename Length
- **MAXIMUM: 32 characters** (Git branch naming compatibility)
- **Format**: `###_short_name.py`
- **Test**: `len("filename.py") <= 32`

## ✅ Good Examples
```
020_make_design_client_id_null.py    # 30 chars - ✅
021_add_price_element_tpl_id.py      # 28 chars - ✅
022_add_user_permissions.py          # 24 chars - ✅
```

## ❌ Bad Examples
```
020_make_design_client_id_nullable.py                 # 34 chars - ❌
021_add_price_element_template_id_to_product_templates.py  # 54 chars - ❌
```

## 📝 Abbreviation Standards
- `template` → `tpl`
- `element` → `elem`
- `product` → `prod`
- `configuration` → `config`
- `nullable` → `null`
- `foreign_key` → `fk`
- `constraint` → `constr`
- `relationship` → `rel`
- `messaging` → `msg`
- `request` → `req`
- `requests` → `reqs`

## 🔗 Revision Chain Rules

### Before Creating New Migration
1. **Check production database state**:
   ```bash
   docker exec customer-backend_db_1 psql -U postgres -d customer -c "SELECT * FROM alembic_version;"
   ```

2. **Check latest migration file**:
   ```bash
   ls -la alembic/versions/ | tail -5
   cat alembic/versions/[latest_file].py | head -15
   ```

3. **Verify down_revision matches exactly**

### Migration File Template
```python
"""Brief description under 60 chars

Revision ID: ###_short_name
Revises: [EXACT_PREVIOUS_REVISION_ID]
Create Date: YYYY-MM-DD HH:MM:SS.SSSSSS

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '###_short_name'          # ≤ 32 chars
down_revision = '[PREVIOUS_REV_ID]'  # EXACT match
branch_labels = None
depends_on = None
```

## 🔧 PostgreSQL ENUM Best Practices

When creating tables with ENUM columns, always use `create_type=False` in the column definition to prevent duplicate type errors:

```python
def upgrade() -> None:
    # First, create the enum type explicitly
    my_enum = sa.Enum('value1', 'value2', name='my_enum_type')
    my_enum.create(op.get_bind(), checkfirst=True)

    # Then use create_type=False in the column definition
    my_enum_col = sa.Enum('value1', 'value2', name='my_enum_type', create_type=False)

    op.create_table(
        'my_table',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('status', my_enum_col, nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
```

## 🧪 Testing Strategy

### Local Testing
```bash
# Test migration chain
docker exec backend-api-1 alembic current
docker exec backend-api-1 alembic upgrade head

# Verify no character limit issues
python -c "print(len('your_revision_id'))"

# Run compliance checker
python check_alembic_compliance.py
```

### Production Sync
```bash
# Check production state
docker exec customer-backend_db_1 psql -U postgres -d customer -c "SELECT * FROM alembic_version;"

# If mismatch, update local database to match production
docker exec backend-db-1 psql -U postgres -d customer -c "UPDATE alembic_version SET version_num = 'production_revision_id';"
```

## 🚫 Common Mistakes to Avoid

1. **Long revision IDs** → Database varchar(32) errors
2. **Long filenames** → Git branch naming issues
3. **Wrong down_revision** → Migration chain breaks
4. **Not checking production state** → Deployment failures
5. **Using `git add --all`** → Commits cache files
6. **Not using create_type=False** → Duplicate ENUM type errors

## 🔄 Emergency Recovery

### If Migration Fails in Production
1. Check exact error message for character limits
2. Create new migration with shortened revision IDs
3. Update both current and referencing migrations
4. Test locally before deploying

### If Database State Mismatch
```bash
# Option 1: Update database to match code
UPDATE alembic_version SET version_num = 'new_revision_id';

# Option 2: Update code to match database
# Edit migration files to use database revision ID
```

### If Orphaned Revision (Can't locate revision 'xxx')
This error occurs when the database has a revision ID that doesn't exist in the codebase.

**Fix Pattern:**
1. Create a "bridge" migration file for the orphaned revision:
```python
"""orphan_bridge - Bridge for orphaned revision

Revision ID: [ORPHANED_ID]
Revises: [PREVIOUS_VALID_REVISION]
"""
revision = '[ORPHANED_ID]'
down_revision = '[PREVIOUS_VALID_REVISION]'

def upgrade() -> None:
    pass  # No-op

def downgrade() -> None:
    pass  # No-op
```

2. Update the next migration's `down_revision` to point to the orphaned ID
3. Make migrations **idempotent** to handle schema state inconsistencies

### Making Migrations Idempotent
Always add existence checks to prevent failures on re-run:

```python
from sqlalchemy import inspect

def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()

def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)

def upgrade() -> None:
    if table_exists('my_table'):
        return  # Already applied
    op.create_table('my_table', ...)
```

## 📋 Pre-Deployment Checklist

- [ ] Revision ID ≤ 32 characters
- [ ] Filename ≤ 32 characters
- [ ] down_revision matches previous migration exactly
- [ ] Tested locally with `alembic upgrade head`
- [ ] Checked production database state
- [ ] No cache files committed (*.pyc, __pycache__)
- [ ] ENUM types use create_type=False in column definitions
- [ ] Migrations are idempotent (check if tables/indexes exist before creating)

---

**Last Updated**: 2026-02-05
**Context**: Versya Customer Backend Alembic Migrations
