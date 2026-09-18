#!/usr/bin/env python3
"""
Log Cleanup Tool.

This module provides an object oriented approach to removing outdated log files.
It reads retention policies and log directories from the centralized configuration.
"""

import os
import time

from howlplane.control_plane.config_loader import load_config


class LogCleaner:
    """
    Manages the cleanup of old log files based on retention policies.
    """

    def __init__(self):
        """
        Initializes the LogCleaner and loads configuration.
        """
        from howlplane.control_plane.config_loader import ConfigLoader
        self.repo_root = ConfigLoader().get_repo_root()
        self.config = load_config().get("logs", {})

        # Load directory and retention configuration with fallbacks
        self.log_rel_dir = self.config.get(
            "log_dir", os.path.join("infrastructure", "logs")
        )
        self.log_dir = os.path.join(self.repo_root, self.log_rel_dir)
        self.retention_days = self.config.get("retention_days", 7)
        self.retention_seconds = self.retention_days * 24 * 60 * 60

    def clean_logs(self):
        """
        Iterates over the log directory and removes files older than the retention period.
        """
        if not os.path.exists(self.log_dir):
            print("Log directory does not exist.")
            return

        current_time = time.time()

        for filename in os.listdir(self.log_dir):
            filepath = os.path.join(self.log_dir, filename)
            if os.path.isfile(filepath):
                file_mod_time = os.path.getmtime(filepath)
                if current_time > file_mod_time + self.retention_seconds:
                    os.remove(filepath)
                    print(f"Removed old log: {filename}")

        print("Log cleanup complete.")


def main():
    """
    Main entry point for the log cleanup script.
    """
    cleaner = LogCleaner()
    cleaner.clean_logs()


if __name__ == "__main__":
    main()
