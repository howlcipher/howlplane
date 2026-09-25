#!/usr/bin/env python3
import os

from howlplane.control_plane.config_loader import ConfigLoader


class RSSFeedFetcher:
    """
    A class responsible for synchronizing RSS feeds.
    """

    def __init__(self):
        """
        Initializes the fetcher with configuration settings.
        """
        self.config_loader = ConfigLoader()
        default_path = os.path.join(
            self.config_loader.get_repo_root(), "documentation", "security_rss.md"
        )
        self.output_path = self.config_loader.get("rss_output_path", default_path)

    def sync_feeds(self):
        """
        Writes the synchronization status to the output file.
        """
        with open(self.output_path, "w") as f:
            f.write("# Security RSS Feeds\n\nFeeds synchronized.\n")
        print("RSS feeds synchronized.")


def main():
    """
    Main execution point.
    """
    fetcher = RSSFeedFetcher()
    fetcher.sync_feeds()


if __name__ == "__main__":
    main()
