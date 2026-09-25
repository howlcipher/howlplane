#!/usr/bin/env python3
"""
Module for pulling documents from Google Docs and syncing them locally.
"""

import os
import sys

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Error: Missing Google API libraries.")
    print("Please run 'pip install -r requirements.txt' first.")
    sys.exit(1)


class GoogleDocsPuller:
    """
    Handles authenticating and pulling documents from Google Docs.
    """

    SCOPES = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    def __init__(self):
        """
        Initializes the GoogleDocsPuller with necessary paths.
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.repo_root = os.path.dirname(script_dir)
        self.token_path = os.path.join(self.repo_root, "token.json")
        self.personal_docs_dir = os.path.join(self.repo_root, "personal_docs")

    def setup_directories(self) -> None:
        """
        Creates the output directory if it does not exist.
        """
        if not os.path.exists(self.personal_docs_dir):
            os.makedirs(self.personal_docs_dir)

    def authenticate(self) -> Credentials:
        """
        Authenticates with Google using the token.json file.

        Returns:
            Credentials: The authorized Google credentials.
        """
        if not os.path.exists(self.token_path):
            print("Error: 'token.json' not found. You must authenticate first.")
            print("Please run 'python scripts/setup_google_docs_auth.py'")
            sys.exit(1)

        return Credentials.from_authorized_user_file(self.token_path, self.SCOPES)

    def pull_documents(self) -> None:
        """
        Fetches the most recent Google Docs and saves them as plain text locally.
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

        self.setup_directories()
        creds = self.authenticate()

        try:
            drive_service = build("drive", "v3", credentials=creds)

            print("Fetching your most recent Google Docs...")
            # Search for Google Docs only
            query = "mimeType='application/vnd.google-apps.document'"
            results = (
                drive_service.files()
                .list(
                    q=query,
                    pageSize=10,
                    fields="nextPageToken, files(id, name)",
                    orderBy="modifiedTime desc",
                )
                .execute()
            )

            items = results.get("files", [])

            if not items:
                print("No Google Docs found.")
                return

            print(
                f"Found {len(items)} documents. Syncing to 'personal_docs/' directory..."
            )

            for item in items:
                doc_id = item["id"]
                doc_name = item["name"].replace("/", "_")

                # Export the document as plain text
                request = drive_service.files().export_media(
                    fileId=doc_id, mimeType="text/plain"
                )
                content = request.execute()

                file_path = os.path.join(self.personal_docs_dir, f"{doc_name}.txt")
                with open(file_path, "wb") as f:
                    f.write(content)

                print(f"  ✓ Synced: {doc_name}")

            print(
                f"\nSuccess! Your documents have been safely synced to {self.personal_docs_dir}/"
            )
            print(
                "This folder is explicitly git-ignored to guarantee your personal data is never pushed to GitHub."
            )

        except HttpError as error:
            print(f"An error occurred: {error}")
            print(
                "If you recently updated scopes, you may need to delete 'token.json' and re-authenticate."
            )
            sys.exit(1)


def main():
    """
    Main entry point for the script.
    """
    puller = GoogleDocsPuller()
    puller.pull_documents()


if __name__ == "__main__":
    main()
