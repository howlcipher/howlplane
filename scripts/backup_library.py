#!/usr/bin/env python3
"""
Library Backup Tool.

This module provides an object oriented approach to backing up the library.
It utilizes centralized configuration where possible to determine backup targets
and destinations.
"""

import os
import tarfile

from howlplane.control_plane.config_loader import load_config


class LibraryBackupManager:
    """
    Manages the creation of library backups.
    """

    def __init__(self):
        """
        Initializes the LibraryBackupManager by loading configuration
        and setting up paths.
        """
        from howlplane.control_plane.config_loader import ConfigLoader

        self.repo_root = ConfigLoader().get_repo_root()
        loader = load_config()
        self.config = loader.get("backup", {})
        self.db_mode = loader.get("database", {}).get("mode", "sqlite")
        self.pg_dsn = loader.get("database", {}).get(
            "pgvector_dsn", "postgresql://localhost:5432/ai_knowledge"
        )

        # Read from config with fallbacks
        self.backup_rel_dir = self.config.get(
            "backup_dir", os.path.join("infrastructure", "backups")
        )
        self.backup_dir = os.path.join(self.repo_root, self.backup_rel_dir)
        self.backup_filename = self.config.get("filename", "library_backup.tar.gz")
        self.targets = self.config.get("targets", ["documentation"])

    def create_backup(self):
        """
        Executes the backup process, compressing target directories into a tar archive.
        """
        import shutil
        import subprocess

        from howlplane.control_plane.config_loader import get_chroma_db_path

        os.makedirs(self.backup_dir, exist_ok=True)
        out_path = os.path.join(self.backup_dir, self.backup_filename)

        with tarfile.open(out_path, "w:gz") as tar:
            for target in self.targets:
                target_path = os.path.join(self.repo_root, target)
                if os.path.exists(target_path):
                    tar.add(target_path, arcname=target)
                    print(f"Added {target} to backup.")
                else:
                    print(f"Target directory {target_path} does not exist. Skipping.")

            # Backup database based on mode
            if self.db_mode == "sqlite" or self.db_mode == "chroma":
                chroma_path = get_chroma_db_path()
                if os.path.exists(chroma_path):
                    tar.add(chroma_path, arcname=".chromadb")
                    print("Added ChromaDB to backup.")
            elif self.db_mode == "pgvector":
                pg_dump_path = os.path.join(self.backup_dir, "pg_dump.sql")
                pg_dump_exe = shutil.which("pg_dump")
                if not pg_dump_exe:
                    print("Failed to backup pgvector: pg_dump not found in PATH.")
                else:
                    try:
                        subprocess.run(
                            [pg_dump_exe, self.pg_dsn, "-f", pg_dump_path], check=True
                        )
                        tar.add(pg_dump_path, arcname="pg_dump.sql")
                        os.remove(pg_dump_path)
                        print("Added PostgreSQL dump to backup.")
                    except Exception as e:
                        print(f"Failed to backup pgvector database: {e}")

        print("Backup completed successfully.")


def main():
    """
    Main entry point for the backup script.
    """
    manager = LibraryBackupManager()
    manager.create_backup()


if __name__ == "__main__":
    main()
