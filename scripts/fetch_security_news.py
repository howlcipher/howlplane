#!/usr/bin/env python3
import json
import os
import urllib.request
from datetime import datetime

from howlplane.control_plane.config_loader import ConfigLoader


class SecurityNewsFetcher:
    """
    Class to fetch the latest security news and vulnerability scores.
    """

    def __init__(self):
        """
        Initializes the SecurityNewsFetcher with configuration settings.
        """
        self.config_loader = ConfigLoader()
        self.url = self.config_loader.get(
            "epss_api_url", "https://api.first.org/data/v1/epss"
        )

        default_path = os.path.join(
            self.config_loader.get_repo_root(), "documentation", "security_news.md"
        )
        self.news_path = self.config_loader.get("security_news_path", default_path)

    def fetch_news(self):
        """
        Fetches the news from the API and writes it to the output file.
        """
        req = urllib.request.Request(self.url)
        try:
            with urllib.request.urlopen(req) as response:  # nosec B310
                if response.status == 200:
                    data = json.loads(response.read().decode("utf_8"))
                    self._write_news(data)
                else:
                    print("Failed to fetch news.")
        except Exception as e:
            print(f"Error: {e}")

    def _write_news(self, data):
        """
        Writes the fetched data to the markdown file.
        """
        with open(self.news_path, "w") as f:
            f.write("# Latest Security Vulnerability Scores\n\n")
            f.write(f"Updated: {datetime.now().isoformat()}\n\n")
            for item in data.get("data", [])[:10]:
                cve = item.get("cve", "Unknown")
                epss = item.get("epss", "Unknown")
                f.write(f"* {cve}: EPSS Score {epss}\n")
        print("Security news updated successfully.")


def main():
    """
    Main function to execute the news fetcher.
    """
    fetcher = SecurityNewsFetcher()
    fetcher.fetch_news()


if __name__ == "__main__":
    main()
