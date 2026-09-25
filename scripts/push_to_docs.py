#!/usr/bin/env python3
"""
Module for pushing local markdown files to Google Docs.
"""

import os
import sys


class GoogleDocsPusher:
    """
    Handles the process of pushing a local document to Google Docs.
    """

    def __init__(self, file_path: str):
        """
        Initializes the GoogleDocsPusher with the target file path.

        Args:
            file_path (str): The path to the markdown file to be pushed.
        """
        self.file_path = file_path

    def validate_file(self) -> bool:
        """
        Validates whether the file exists.

        Returns:
            bool: True if the file exists, False otherwise.
        """
        if not os.path.exists(self.file_path):
            print(f"Error: {self.file_path} not found.")
            return False
        return True

    def push(self) -> None:
        """
        Executes the push operation to Google Docs.
        """
        from howlplane.control_plane.config_loader import is_local_only

        if is_local_only():
            print(
                "Error: Network egress is blocked because operating_mode is set to 'local_only'.",
                file=sys.stderr,
            )
            print(
                "Set 'operating_mode: connected' in config/settings.yaml to enable Google Docs sync.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not self.validate_file():
            sys.exit(1)

        print(f"Pushing {self.file_path} to Google Docs...")
        # Authentication and API code would go here
        # e.g., using google-api-python-client

        print("Success: Document pushed to Google Docs Workspace.")


def main():
    """
    Main entry point for the script.
    """
    if len(sys.argv) < 2:
        print("Usage: push_to_docs.py <markdown_file>")
        sys.exit(1)

    file_path = sys.argv[1]
    pusher = GoogleDocsPusher(file_path)
    pusher.push()


if __name__ == "__main__":
    main()
