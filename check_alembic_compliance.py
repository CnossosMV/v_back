#!/usr/bin/env python3
"""
Alembic Migration Compliance Checker
Validates migration files against ALEMBIC_MIGRATION_RULES.md constraints
"""

import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict


class AlembicComplianceChecker:
    def __init__(self, backend_dir: str = "."):
        self.backend_dir = Path(backend_dir)
        self.violations = []

    def check_filename_lengths(self) -> List[Dict]:
        """Check migration filename lengths (max 32 chars)"""
        violations = []
        versions_dir = self.backend_dir / "alembic" / "versions"

        if not versions_dir.exists():
            return [{"type": "error", "message": f"Alembic versions directory not found: {versions_dir}"}]

        for migration_file in versions_dir.glob("*.py"):
            filename = migration_file.name
            if len(filename) > 32:
                violations.append({
                    "type": "filename_too_long",
                    "file": filename,
                    "length": len(filename),
                    "max_allowed": 32,
                    "message": f"Filename '{filename}' is {len(filename)} chars (max: 32)"
                })

        return violations

    def check_revision_ids(self) -> List[Dict]:
        """Check revision ID lengths (max 32 chars)"""
        violations = []
        versions_dir = self.backend_dir / "alembic" / "versions"

        if not versions_dir.exists():
            return [{"type": "error", "message": f"Alembic versions directory not found: {versions_dir}"}]

        for migration_file in versions_dir.glob("*.py"):
            try:
                with open(migration_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Extract revision ID
                revision_match = re.search(r"revision = ['\"]([^'\"]+)['\"]", content)
                if revision_match:
                    revision_id = revision_match.group(1)
                    if len(revision_id) > 32:
                        violations.append({
                            "type": "revision_id_too_long",
                            "file": migration_file.name,
                            "revision_id": revision_id,
                            "length": len(revision_id),
                            "max_allowed": 32,
                            "message": f"Revision ID '{revision_id}' in {migration_file.name} is {len(revision_id)} chars (max: 32)"
                        })
                else:
                    violations.append({
                        "type": "revision_id_not_found",
                        "file": migration_file.name,
                        "message": f"No revision ID found in {migration_file.name}"
                    })

            except Exception as e:
                violations.append({
                    "type": "file_read_error",
                    "file": migration_file.name,
                    "message": f"Error reading {migration_file.name}: {str(e)}"
                })

        return violations

    def check_docker_constraints(self) -> List[Dict]:
        """Check if Docker/PostgreSQL constraints are enforced"""
        violations = []

        # Try different container names
        container_names = [
            "customer-db-1",
            "backend-db-1",
            "customer-backend_db_1",
            "versya-db-1"
        ]

        db_container = None
        for container in container_names:
            try:
                result = subprocess.run(
                    ["docker", "ps", "--filter", f"name={container}", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=5
                )
                if container in result.stdout:
                    db_container = container
                    break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        if not db_container:
            violations.append({
                "type": "docker_not_running",
                "message": "No database container found. Skipping database constraint check."
            })
            return violations

        try:
            result = subprocess.run([
                "docker", "exec", db_container, "psql", "-U", "postgres", "-d", "versya",
                "-c", "\\d alembic_version"
            ], capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                if "character varying(32)" not in result.stdout:
                    violations.append({
                        "type": "database_constraint_mismatch",
                        "message": "alembic_version.version_num column doesn't have varchar(32) constraint",
                        "details": result.stdout
                    })
            else:
                violations.append({
                    "type": "database_connection_error",
                    "message": f"Cannot connect to database: {result.stderr}"
                })

        except subprocess.TimeoutExpired:
            violations.append({
                "type": "database_timeout",
                "message": "Database connection timeout"
            })
        except FileNotFoundError:
            violations.append({
                "type": "docker_not_found",
                "message": "Docker command not found. Please ensure Docker is installed and running."
            })

        return violations

    def suggest_fixes(self, violations: List[Dict]) -> List[Dict]:
        """Suggest fixes for violations"""
        suggestions = []

        for violation in violations:
            if violation["type"] == "filename_too_long":
                filename = violation["file"]
                shortened = self.apply_abbreviations(filename)
                suggestions.append({
                    "violation": violation,
                    "fix": f"Rename '{filename}' to '{shortened}'",
                    "command": f"mv alembic/versions/{filename} alembic/versions/{shortened}"
                })

            elif violation["type"] == "revision_id_too_long":
                revision_id = violation["revision_id"]
                shortened = self.apply_abbreviations(revision_id)
                suggestions.append({
                    "violation": violation,
                    "fix": f"Change revision ID from '{revision_id}' to '{shortened}'",
                    "command": f"# Edit {violation['file']} and change revision ID to '{shortened}'"
                })

        return suggestions

    def apply_abbreviations(self, text: str) -> str:
        """Apply abbreviation rules from ALEMBIC_MIGRATION_RULES.md"""
        abbreviations = {
            'template': 'tpl',
            'element': 'elem',
            'product': 'prod',
            'configuration': 'config',
            'nullable': 'null',
            'foreign_key': 'fk',
            'constraint': 'constr',
            'relationship': 'rel',
            'templates': 'tpls',
            'elements': 'elems',
            'products': 'prods',
            'messaging': 'msg',
            'request': 'req',
            'requests': 'reqs'
        }

        result = text
        for full, abbrev in abbreviations.items():
            result = result.replace(full, abbrev)

        return result

    def run_compliance_check(self) -> Dict:
        """Run all compliance checks"""
        all_violations = []

        print("🔍 Checking Alembic Migration Compliance...")
        print("=" * 50)

        # Check filename lengths
        print("\n📁 Checking filename lengths...")
        filename_violations = self.check_filename_lengths()
        all_violations.extend(filename_violations)

        if filename_violations:
            print(f"❌ Found {len(filename_violations)} filename violations")
            for v in filename_violations:
                print(f"   - {v['message']}")
        else:
            print("✅ All filenames comply with length requirements")

        # Check revision ID lengths
        print("\n🔖 Checking revision ID lengths...")
        revision_violations = self.check_revision_ids()
        all_violations.extend(revision_violations)

        if revision_violations:
            print(f"❌ Found {len(revision_violations)} revision ID violations")
            for v in revision_violations:
                print(f"   - {v['message']}")
        else:
            print("✅ All revision IDs comply with length requirements")

        # Check Docker constraints
        print("\n🐳 Checking Docker/PostgreSQL constraints...")
        docker_violations = self.check_docker_constraints()
        all_violations.extend(docker_violations)

        if docker_violations:
            print(f"⚠️  Found {len(docker_violations)} Docker/database issues")
            for v in docker_violations:
                print(f"   - {v['message']}")
        else:
            print("✅ Database constraints are properly configured")

        # Generate suggestions
        if all_violations:
            suggestions = self.suggest_fixes(all_violations)

            if suggestions:
                print(f"\n🔧 Generating fix suggestions...")
                print(f"Found {len(suggestions)} suggested fixes:")
                for i, suggestion in enumerate(suggestions, 1):
                    print(f"\n{i}. {suggestion['fix']}")
                    if 'command' in suggestion:
                        print(f"   Command: {suggestion['command']}")

        return {
            "total_violations": len(all_violations),
            "violations": all_violations,
            "suggestions": self.suggest_fixes(all_violations) if all_violations else []
        }


def main():
    checker = AlembicComplianceChecker()
    result = checker.run_compliance_check()

    print(f"\n" + "=" * 50)
    if result["total_violations"] == 0:
        print("🎉 All checks passed! Your migrations are compliant.")
        exit(0)
    else:
        print(f"❌ Found {result['total_violations']} violations that need attention.")
        print("\nRefer to ALEMBIC_MIGRATION_RULES.md for guidance on fixing these issues.")
        exit(1)


if __name__ == "__main__":
    main()
