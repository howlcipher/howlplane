#!/usr/bin/env python3
import json
import os
import sys
import urllib.request

from howlplane.control_plane.config_loader import ConfigLoader


class GitHubProfileSyncer:
    """
    Class responsible for synchronizing recent GitHub projects to a user profile.
    """

    def __init__(self):
        """
        Initializes the syncer with configuration settings.
        """
        self.config_loader = ConfigLoader()
        self.username = self.config_loader.get("github_username", "howlcipher")
        self.api_url = self.config_loader.get(
            "github_api_base_url", "https://api.github.com/users/"
        )

        default_path = os.path.join(
            self.config_loader.get_repo_root(), "USER_PROFILE.md"
        )
        self.profile_path = self.config_loader.get("user_profile_path", default_path)

        self.header_key = "User-Agent"
        self.header_value = "KnowledgeLibrary_Sync"

    def fetch_github_repos(self):
        """
        Fetches the most recently updated repositories for the user.
        """
        url = f"{self.api_url}{self.username}/repos?sort=updated"
        req = urllib.request.Request(url, headers={self.header_key: self.header_value})
        try:
            with urllib.request.urlopen(req) as response:  # nosec B310
                if response.status == 200:
                    data = json.loads(response.read().decode("utf_8"))
                    return data
        except Exception as e:
            print(f"Error fetching data: {e}")
        return []

    def sync_profile(self):
        """
        Parses repository data and appends it to the user profile document.
        """
        print(f"Fetching recent repositories for {self.username}...")
        repos = self.fetch_github_repos()

        if not repos:
            print("No repositories found or sync failed.")
            sys.exit(1)

        output_lines = ["\n## Recent GitHub Projects\n"]
        for repo in repos[:5]:
            name = repo.get("name", "Unknown")
            desc = repo.get("description", "No description provided.")
            if desc is None:
                desc = "No description provided."
            url = repo.get("html_url", "")

            # Remove punctuation characters properly to comply with formatting rules.
            dash = "-"
            desc = desc.replace(dash, " ")
            output_lines.append(f"* **{name}**: {desc} ({url})")

        if os.path.exists(self.profile_path):
            with open(self.profile_path, "a") as f:
                f.write("\n" + "\n".join(output_lines) + "\n")
            print("Successfully synced GitHub projects to USER_PROFILE.md.")
        else:
            print("USER_PROFILE.md not found.")


def main():
    """
    Main function to execute the GitHub profile syncer.
    """
    syncer = GitHubProfileSyncer()
    syncer.sync_profile()


if __name__ == "__main__":
    main()
